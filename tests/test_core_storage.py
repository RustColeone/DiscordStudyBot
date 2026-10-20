"""Storage the core keeps for modules (contracts 1.5): scoped stores, history search and summaries, diary scopes."""

import tempfile
import unittest
from pathlib import Path

from contracts import Address, Sender
from core.manager.handles import HistoryHandle, module_store
from tests.core_support import FakeAdapter, iface, settle, start_core


def turn(message="", status="complete", diary=None):
    return {"message": message, "status": status, "actions": [], **({"diary": diary} if diary else {})}


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, overrides=None, responses=()):
        core = await start_core(self.tmp, overrides, FakeAdapter(list(responses)))
        self.cores.append(core)
        return core

    # ---- one store, any combination of conversation, persona and person ----------------------------
    async def test_each_scope_is_its_own_store_and_modules_never_share(self):
        core = await self.core()
        room, other = Address("chat", "room"), Address("chat", "other")
        alice, bob = Sender("alice"), Sender("bob")

        def store(module="room", address=room, sender=alice, **scope):
            return module_store(core.manager, module, address, sender, ["luna", "gemini"],
                                **{"conversation": False, "persona": None, "person": False, **scope})
        store().set("k", "module")
        store(conversation=True).set("k", "room")
        store(persona="luna").set("k", "luna")
        store(person=True).set("k", "alice")
        store(persona="luna", person=True).set("k", "luna-alice")
        self.assertEqual(store(module="web").get("k"), None)  # another module's store is its own
        self.assertEqual(store(address=other).get("k"), "module")  # module-wide: every conversation
        self.assertEqual(store(address=other, conversation=True).get("k"), None)
        self.assertEqual(store(address=other, persona="luna").get("k"), "luna")  # persona: across conversations
        self.assertEqual(store(sender=bob, person=True).get("k"), None)
        self.assertEqual(store(address=other, person=True).get("k"), "alice")  # person: across conversations
        self.assertEqual(store(persona="luna", person=True).get("k"), "luna-alice")
        self.assertEqual(core.tank.store("extension:room").get("k"), "module")  # no scope = ToolContext.storage
        # A conversation ID may contain anything, even what looks like another scope's name.
        tricky = Address("chat", 'room|persona:luna')
        store(conversation=True, persona="luna").set("t", "luna in room")
        self.assertIsNone(store(address=tricky, conversation=True).get("t"))
        store(sender=Sender('x", "person'), person=True).set("t", "x")
        self.assertIsNone(store(sender=Sender("x"), person=True).get("t"))
        with self.assertRaises(ValueError):
            store(persona="claude")  # not one of the run's personas
        with self.assertRaises(ValueError):
            store(sender=Sender(""), person=True)  # a routine without a person

    # ---- history: search beyond the window, and a rolling summary ----------------------------------
    async def test_history_is_searchable_and_keeps_one_scrubbed_summary(self):
        core = await self.core()
        for text in ("we picked Paris for the trip", "50% off_sale today", "nothing here"):
            core.tank.record_message("chat:room", "in", text, sender_id="alice", sender_name="Alice")
        history = HistoryHandle(core.manager, Address("chat", "room"))
        self.assertEqual([item.text for item in history.search("paris")], ["we picked Paris for the trip"])
        self.assertEqual([item.text for item in history.search("50%")], ["50% off_sale today"])  # % and _ are literal
        self.assertEqual(history.search("off_s")[0].text, "50% off_sale today")
        self.assertEqual(history.search("x_y"), [])
        for text in ("Le CAFÉ du coin", "Встреча в Москве"):
            core.tank.record_message("chat:room", "in", text, sender_id="alice")
        self.assertEqual([item.text for item in history.search("café")], ["Le CAFÉ du coin"])  # beyond ASCII
        self.assertEqual([item.text for item in history.search("москве")], ["Встреча в Москве"])
        self.assertIsNone(history.summary())
        from core.manager.secrets import Secrets
        core.manager.secrets = Secrets({"TEST_TOKEN": "very-secret-value"})
        history.summarize("They chose Paris. Token very-secret-value.", upto=123.0)
        summary = history.summary()
        self.assertEqual((summary.upto, "very-secret-value" in summary.text), (123.0, False))
        self.assertIsNone(HistoryHandle(core.manager, Address("chat", "other")).summary())  # per conversation
        with self.assertRaises(ValueError):
            history.summarize("x" * 8001, upto=1.0)

    async def test_models_can_search_this_conversation_only(self):
        core = await self.core()
        core.tank.record_message("chat:room", "in", "the wifi password is on the fridge", sender_name="Alice")
        core.tank.record_message("chat:other", "in", "the fridge is broken", sender_name="Bob")
        await iface(core).say('tool core.search_history {"query": "fridge"}')
        await settle(core)
        answer = iface(core).texts("room")[-1]
        self.assertIn("Alice: the wifi password is on the fridge", answer)
        self.assertNotIn("broken", answer)

    async def test_memory_shows_this_chats_summary_and_only_admins_clear_a_shared_one(self):
        core = await self.core()
        HistoryHandle(core.manager, Address("chat", "room")).summarize("We planned the Paris trip.", upto=0.0)
        await iface(core).say("$memory summary", is_direct=False, mentions_me=True)
        await settle(core)
        self.assertIn("We planned the Paris trip.", iface(core).texts("room")[-1])
        await iface(core).say("$memory clear summary", is_direct=False, mentions_me=True)  # alice, a member, in a shared chat
        await settle(core)
        self.assertIn("Only admins", iface(core).texts("room")[-1])
        await iface(core).say("$memory clear summary", is_direct=False, mentions_me=True, sender="boss")
        await settle(core)
        self.assertIsNone(HistoryHandle(core.manager, Address("chat", "room")).summary())

    async def test_search_finds_earlier_messages_only_and_survives_a_broken_time(self):
        core = await self.core()
        core.tank.record_message("chat:room", "in", "we went to Paris in May", sender_name="Bob", at=1000.0)
        core.tank.record_message("chat:room", "in", "Paris again, oddly timed", sender_name="Eve", at=1e20)
        await iface(core).say('tool core.search_history {"query": "Paris", "limit": 5}')
        await settle(core)
        answer = iface(core).texts("room")[-1]
        self.assertNotIn("core.search_history", answer)  # not the question that asked
        self.assertIn("Bob: we went to Paris in May", answer)
        self.assertIn("[unknown time] Eve: Paris again", answer)
        history = HistoryHandle(core.manager, Address("chat", "room"))
        self.assertEqual(len(history.search("paris", limit=None)), 4)  # odd limit: the default; with Q and A

    async def test_search_matches_letters_however_they_are_written(self):
        core = await self.core()
        for text in ("İstanbul trip", "cafe\u0301 au lait", "ＦＵＬＬ width", "Straße"):
            core.tank.record_message("chat:room", "in", text, sender_id="alice")
        history = HistoryHandle(core.manager, Address("chat", "room"))
        for query, found in (("istanbul", "İstanbul trip"), ("café", "cafe\u0301 au lait"), ("full", "ＦＵＬＬ width"),
                             ("strasse", "Straße")):
            self.assertEqual([item.text for item in history.search(query)], [found], query)

    async def test_a_summary_needs_a_real_time_and_stays_within_its_limit(self):
        core = await self.core()
        from core.manager.secrets import Secrets
        core.manager.secrets = Secrets({"A_VERY_LONG_SECRET_NAME_FOR_TEST": "Zq9xT2mWp7"})
        history = HistoryHandle(core.manager, Address("chat", "room"))
        for upto in (float("nan"), float("inf"), "soon"):
            with self.assertRaises(ValueError):
                history.summarize("A summary.", upto)
        with self.assertRaises(ValueError):
            history.summarize(["not", "text"], 1.0)
        history.summarize("Zq9xT2mWp7 " * 727, upto=1.0)  # 7997 characters, longer once scrubbed
        self.assertEqual(len(history.summary().text), 8000)
        core.tank.store("core:summaries").set("chat:room", {"text": "Old.", "upto": 1e20, "at": 0.0})
        await iface(core).say("$memory summary")
        await settle(core)
        self.assertIn("up to unknown time", iface(core).texts("room")[-1])

    async def test_a_routine_a_model_set_up_cannot_erase_memories(self):
        import time
        now = [time.time()]
        core = await start_core(self.tmp, {"permissions": {"*": {"rules": {"core.add_routine": "auto"}}}},
                                FakeAdapter([]), clock=lambda: now[0])
        self.cores.append(core)
        HistoryHandle(core.manager, Address("chat", "group")).summarize("Long shared history.", upto=1.0)
        core.tank.save_note("p", "chat:group", "boss", "k", "fact", "Boss likes tea", 3, 200)
        for text in ("$memory clear summary", "$memory clear", "$memory forget 1"):
            await iface(core).say(f'tool core.add_routine {{"schedule": "every 1h", "text": "{text}"}}', sender="boss",
                                  conversation="group", is_direct=False, mentions_me=True, name="Boss")
            await settle(core)
        now[0] += 61 * 60
        await core.gateway.tick(now[0])
        await settle(core)
        refused = [text for text in iface(core).texts("group") if "A routine set up through a tool cannot use" in text]
        self.assertEqual(len(refused), 3)
        self.assertIsNotNone(HistoryHandle(core.manager, Address("chat", "group")).summary())
        self.assertEqual(len(core.tank.notes("p", "chat:group", "boss")), 1)

    # ---- diaries: filed where written; a scope reads several conversations' notes together ----------
    NOTE = {"key": "food.favorite", "kind": "fact", "note": "Likes spicy noodles", "importance": 4}

    def shared(self, *addresses, scope="mine"):
        rule = {"engine": "assistant", "personas": ["p"], "options": {"diary_scope": scope}}
        return {"assignments": {"*": {"engine": "assistant", "personas": ["p"]},
                                **{address: rule for address in addresses}}}

    async def test_a_diary_scope_reads_its_conversations_notes_together(self):
        core = await self.core(self.shared("chat:dm", "chat:group"),
                               [turn("Noted!", diary=[self.NOTE]), turn("Hi."), turn("Hello.")])
        for conversation in ("dm", "group", "elsewhere"):
            await iface(core).say("I love 担担面, the spicy one" if conversation == "dm" else "hello",
                                  conversation=conversation)
            await settle(core)
        adapter = core.providers._adapters["fake"]
        self.assertIn("Likes spicy noodles", adapter.calls[1]["messages"][0]["content"])  # the group reads it
        self.assertNotIn("Likes spicy noodles", adapter.calls[2]["messages"][0]["content"])  # elsewhere does not
        self.assertEqual([n.key for n in core.tank.notes("p", "chat:dm", "alice")], ["food.favorite"])  # filed in dm
        await iface(core).say("$memory", conversation="group")
        await settle(core)
        self.assertIn("Likes spicy noodles", iface(core).texts("group")[-1])
        await iface(core).say("$memory", conversation="elsewhere")
        await settle(core)
        self.assertIn("Nobody keeps notes about you here", iface(core).texts("elsewhere")[-1])

    async def test_changing_a_scope_moves_nothing(self):
        core = await self.core({"assignments": {"*": {"engine": "assistant", "personas": ["p"]}}},
                               [turn("Noted!", diary=[self.NOTE])])
        await iface(core).say("I love 担担面, the spicy one", conversation="dm")
        await settle(core)
        rows = lambda: [(n.id, n.note) for n in core.tank.notes("p", "chat:dm", "alice")]
        before = rows()
        self.assertEqual(len(before), 1)
        for address in ("chat:dm", "chat:group"):  # turned on later: the dm's old note is shared as it is
            core.manager.set_assignment(address, {"engine": "assistant", "personas": ["p"],
                                                  "options": {"diary_scope": "mine"}})
        await iface(core).say("$memory", conversation="group")
        await settle(core)
        self.assertIn("Likes spicy noodles", iface(core).texts("group")[-1])
        core.manager.delete_assignment("chat:group")  # and off again: the group no longer reads it
        await iface(core).say("$memory", conversation="group")
        await settle(core)
        self.assertIn("Nobody keeps notes about you here", iface(core).texts("group")[-1])
        self.assertEqual(rows(), before)  # never moved or copied

    async def test_a_correction_in_one_chat_updates_the_note_where_it_lives(self):
        sushi = {**self.NOTE, "note": "Now prefers sushi over noodles"}
        core = await self.core(self.shared("chat:dm", "chat:group"),
                               [turn("Noted!", diary=[self.NOTE]), turn("Got it!", diary=[sushi])])
        await iface(core).say("I love 担担面, the spicy one", conversation="dm")
        await settle(core)
        await iface(core).say("Actually these days I'd rather have sushi", conversation="group")
        await settle(core)
        self.assertEqual([n.note for n in core.tank.notes("p", ["chat:dm", "chat:group"], "alice")],
                         ["Now prefers sushi over noodles"])  # one note, still filed in the dm
        self.assertEqual(core.tank.notes("p", "chat:group", "alice"), [])
        await iface(core).say("$memory clear", conversation="group")
        await settle(core)
        self.assertIn("Removed 1 note(s) about you in the chats that share this memory", iface(core).texts("group")[-1])
        self.assertEqual(core.tank.notes("p", "chat:dm", "alice"), [])

    async def test_a_shared_diary_keeps_people_of_different_interfaces_apart(self):
        core = await self.core(self.shared("chat:dm", "owner:dm"), [turn("Noted!", diary=[self.NOTE]), turn("Hi.")])
        await iface(core).say("I love 担担面, the spicy one", conversation="dm")  # alice on chat
        await settle(core)
        await iface(core, "owner").say("hello", conversation="dm")  # a different alice, on owner
        await settle(core)
        adapter = core.providers._adapters["fake"]
        self.assertNotIn("Likes spicy noodles", adapter.calls[1]["messages"][0]["content"])
        await iface(core, "owner").say("$memory clear", conversation="dm")
        await settle(core)
        self.assertEqual(len(core.tank.notes("p", "chat:dm", "alice")), 1)  # not hers to clear

    async def test_recall_gives_the_newest_of_a_key_however_many_notes_outrank_it(self):
        from types import SimpleNamespace
        from core.manager.diary import DiaryHandle
        core = await self.core()
        tank = core.tank
        tank.save_note("p", "chat:dm", "alice", "food.favorite", "fact", "Likes spicy noodles", 5, 200)
        for index in range(6):
            tank.save_note("p", "chat:dm", "alice", f"other.{index}", "fact", f"Important {index}", 5, 200)
        tank.save_note("p", "chat:group", "alice", "food.favorite", "fact", "Now prefers sushi", 3, 200)  # newer
        record = SimpleNamespace(id="r", address="chat:dm", origin="conversation")
        diary = DiaryHandle(core.manager, record, "p", "hi", "alice", chats=["chat:dm", "chat:group"])
        recalled = [note.note for note in diary.recall("", limit=50)]
        self.assertIn("Now prefers sushi", recalled)
        self.assertNotIn("Likes spicy noodles", recalled)
        recalled = [note.note for note in diary.recall("food", limit=3)]
        self.assertNotIn("Likes spicy noodles", recalled)

    async def test_notes_the_previous_version_filed_under_a_scope_are_filed_in_a_chat_again(self):
        core = await self.core(self.shared("chat:dm", "chat:group"))
        core.tank.record_message("chat:dm", "in", "hi", sender_id="alice", at=100.0)
        core.tank.record_message("chat:group", "in", "hello", sender_id="alice", at=200.0)  # where she spoke last
        core.tank.save_note("p", ":scope:mine", "chat:alice", "food.favorite", "fact", "Likes spicy noodles", 4, 200)
        core.tank.save_note("p", ":scope:gone", "chat:alice", "old.key", "fact", "From a scope nobody names", 4, 200)
        core.tank.save_note("p", "chat:group", "alice", "food.favorite", "fact", "An older note", 4, 200)
        core.tank.db.execute("UPDATE diary SET updated_at=1 WHERE note='An older note'")
        core.manager._refile_shared_diaries()
        self.assertEqual([n.note for n in core.tank.notes("p", "chat:group", "alice")], ["Likes spicy noodles"])
        self.assertEqual([n.key for n in core.tank.notes("p", ":scope:gone", "chat:alice")], ["old.key"])
        await iface(core).say("$memory", conversation="dm")
        await settle(core)
        self.assertIn("Likes spicy noodles", iface(core).texts("dm")[-1])

    def test_a_scope_name_is_text_and_anything_else_means_none(self):
        from core.manager.manager import Manager
        names = [Manager.diary_scope({"diary_scope": value}) for value in
                 (" mine ", 5, False, True, ["a"], {"a": 1}, None, "")]
        self.assertEqual(names, ["mine", "5", "", "", "", "", "", ""])


if __name__ == "__main__":
    unittest.main()

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

    # ---- diaries: the conversation by default, or a scope several conversations share ------------
    async def test_a_diary_scope_shares_notes_across_its_conversations_only(self):
        shared = {"engine": "assistant", "personas": ["p"], "options": {"diary_scope": "mine"}}
        core = await self.core({"assignments": {"*": {"engine": "assistant", "personas": ["p"]},
                                                "chat:dm": shared, "chat:group": shared}},
                               [turn("Noted!", diary=[{"key": "food.favorite", "kind": "fact",
                                                       "note": "Likes spicy noodles", "importance": 4}]),
                                turn("Hi."), turn("Hello.")])
        for conversation in ("dm", "group", "elsewhere"):
            await iface(core).say("I love 担担面, the spicy one" if conversation == "dm" else "hello",
                                  conversation=conversation)
            await settle(core)
        adapter = core.providers._adapters["fake"]
        self.assertIn("Likes spicy noodles", adapter.calls[1]["messages"][0]["content"])  # the group shares it
        self.assertNotIn("Likes spicy noodles", adapter.calls[2]["messages"][0]["content"])  # elsewhere does not
        self.assertEqual([n.key for n in core.tank.notes("p", "scope:mine", "alice")], ["food.favorite"])
        await iface(core).say("$memory", conversation="group")  # the DM's note, read from the group chat
        await settle(core)
        self.assertIn("Likes spicy noodles", iface(core).texts("group")[-1])
        await iface(core).say("$memory", conversation="elsewhere")
        await settle(core)
        self.assertIn("Nobody keeps notes about you here", iface(core).texts("elsewhere")[-1])


if __name__ == "__main__":
    unittest.main()

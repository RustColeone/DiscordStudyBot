"""Storage the core keeps for modules (contracts 1.5): scoped stores, history search and summaries, diary scopes."""

import tempfile
import unittest
from pathlib import Path

from contracts import Address, Sender
from core.manager.handles import HistoryHandle, module_store
from tests.core_support import FIXTURES, FakeAdapter, iface, make_config, settle, start_core


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
        core.tank.add_log("chat:group", "core", "note", "Boss likes tea", subject="boss", persona="p")
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
        self.assertEqual(len(core.tank.log_lines("chat:group", kind="note", subject="boss")), 1)


if __name__ == "__main__":
    unittest.main()

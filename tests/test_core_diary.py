"""Persona diaries: core rules, the $memory command and the assistant engine's use of them."""

import tempfile
import time
import unittest
from pathlib import Path

from tests.core_support import FakeAdapter, iface, settle, start_core, turn_context


def turn(message="", status="complete", actions=(), diary=None):
    value = {"message": message, "status": status, "actions": [dict(tool=t, arguments=a) for t, a in actions]}
    if diary is not None:
        value["diary"] = diary
    return value


def note(key, text, importance=4, kind="fact"):
    return {"key": key, "kind": kind, "note": text, "importance": importance}


class DiaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, responses=(), overrides=None):
        adapter = FakeAdapter(list(responses))
        core = await start_core(self.tmp, {"assignments": {"*": {"engine": "assistant", "personas": ["p"]}},
                                           "personas": {"q": {"display_name": "Quinn", "prompt": "You are Quinn."}},
                                           **(overrides or {})}, adapter)
        self.cores.append(core)
        return core, adapter

    def context(self, adapter, index=0):
        return turn_context(adapter.calls[index])

    async def test_notes_are_kept_per_person_and_conversation_and_recalled_next_time(self):
        core, adapter = await self.core([
            turn("Nice!", diary=[note("music.favorite", "Loves Andy Hui's songs"), note("mood", "fine", 2)]),
            turn("Hi Bob."), turn("Hi again."), turn("Hello.")])
        for text, options in (("I really love 古巨基", {}), ("hello", {"sender": "bob", "name": "Bob"}),
                              ("any music for me?", {}), ("hi", {"conversation": "elsewhere"})):
            await iface(core).say(text, **options)
            await settle(core)
        self.assertNotIn("Andy Hui", self.context(adapter, 1))
        self.assertIn('"note": "Loves Andy Hui\'s songs"', self.context(adapter, 2))
        self.assertIn("Your diary about Alice", self.context(adapter, 2))
        self.assertNotIn("Andy Hui", self.context(adapter, 3))
        notes = core.tank.notes("p", "chat:room", "alice")
        self.assertEqual([(n.key, n.importance) for n in notes], [("music.favorite", 4)])

    async def test_core_refuses_invalid_copied_excess_and_routine_notes(self):
        from core.manager.diary import WRITES_PER_RUN
        core, adapter = await self.core()
        await iface(core).say("my name is Alice and I study chemistry at the university of somewhere nice")
        await settle(core)
        record = core.tank.runs(("done", "failed"))[0]
        from core.manager.handles import SessionHandle
        session = SessionHandle(core.manager, record, [], False)
        diary = session.diary("p")
        for args in (("Bad Key", "fact", "x", 3), ("k", "rumour", "x", 3), ("k", "fact", "", 3),
                     ("k", "fact", "x" * 401, 3), ("k", "fact", "x", 9), ("k", "fact", "x", True),
                     ("k", "fact", "  my name is Alice and I study chemistry at the university of somewhere nice", 3)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                diary.write(*args)
        for index in range(WRITES_PER_RUN):
            self.assertTrue(diary.write(f"k{index}", "fact", f"note {index}", 3))
        self.assertFalse(diary.write("k0", "fact", "note 0", 3))
        with self.assertRaisesRegex(ValueError, "at most"):
            diary.write("k9", "fact", "one more", 3)
        with self.assertRaises(ValueError):
            session.diary("q")
        from core.manager.diary import DiaryHandle
        record.origin = "routine"
        routine = DiaryHandle(core.manager, record, "p", "", "routine:daily")
        self.assertFalse(routine.available)
        self.assertEqual(routine.recall("note"), [])

    async def test_recall_prefers_matching_notes_and_old_notes_are_trimmed(self):
        core, _ = await self.core()
        for index in range(5):
            core.tank.save_note("p", "chat:room", "alice", f"k{index}", "fact", f"plain {index}", 3, keep=4)
            time.sleep(0.001)
        self.assertEqual(sorted(n.key for n in core.tank.notes("p", "chat:room", "alice")), ["k1", "k2", "k3", "k4"])
        core.tank.save_note("p", "chat:room", "alice", "chem", "fact", "studies 化学 at uni", 3, keep=10)
        await iface(core).say("noop")
        await settle(core)
        from core.manager.handles import SessionHandle
        record = core.tank.runs(("done", "failed"))[0]
        diary = SessionHandle(core.manager, record, [], False).diary("p")
        self.assertEqual(diary.recall("how is my 化学 going", 2)[0].key, "chem")
        self.assertEqual(len(diary.recall("", 50)), 5)

    async def test_memory_command_shows_forgets_and_clears_only_your_own_notes(self):
        core, _ = await self.core()
        tank = core.tank
        tank.save_note("p", "chat:room", "alice", "a", "fact", "likes tea", 4, 200)
        tank.save_note("q", "chat:room", "alice", "b", "fact", "plays go", 3, 200)
        tank.save_note("p", "chat:room", "bob", "c", "fact", "bob note", 3, 200)
        chat = iface(core)
        bob_note = tank.notes("p", "chat:room", "bob")[0].id
        for text in ("$memory", "$memory pat", f"$memory forget {bob_note}", "$memory clear quinn", "$memory",
                     "$memory clear"):
            await chat.say(text)
            await settle(core)
        replies = chat.texts("room")
        self.assertIn("Pat: 1 note(s)", replies[0])
        self.assertIn("Quinn: 1 note(s)", replies[0])
        self.assertIn("Pat's notes about you (page 1/1)", replies[1])
        self.assertIn("likes tea", replies[1])
        self.assertNotIn("bob note", "".join(replies))
        self.assertEqual(replies[2], "No note with that ID about you here.")
        self.assertEqual(replies[3], "Removed 1 note(s) about you here from Quinn.")
        self.assertIn("Pat's notes about you", replies[4])
        self.assertEqual(replies[5], "Removed 1 note(s) about you here.")
        self.assertEqual(tank.note_counts("chat:room", "alice"), {})
        self.assertEqual(tank.note_counts("chat:room", "bob"), {"p": 1})
        await chat.say("$memory", sender="bob", conversation="room")
        await chat.say(f"$memory forget {bob_note}", sender="bob", conversation="room")
        await settle(core)
        self.assertEqual(chat.texts("room")[-1], "Note removed.")

    async def test_diary_can_be_switched_off_per_assignment(self):
        core, adapter = await self.core([turn("ok", diary=[note("k", "something real")])], {
            "assignments": {"*": {"engine": "assistant", "personas": ["p"], "options": {"diary": False}}}})
        await iface(core).say("I moved to Tokyo")
        await settle(core)
        self.assertNotIn("Your diary", self.context(adapter))
        self.assertEqual(core.tank.note_counts("chat:room", "alice"), {})


if __name__ == "__main__":
    unittest.main()

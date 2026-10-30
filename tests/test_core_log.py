"""The log (contracts 1.15, core/manager/logbook.py): what happened in a conversation, written by modules and by the
core after each write that ran, read by engines and by people with `$log`. Before it, the diary: its notes carry over."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.manager.logbook import LINES_KEPT, LogbookHandle, search_terms
from core.storage.tank import Tank
from tests.core_support import FakeAdapter, iface, settle, start_core, turn_context

ANSWER = {"message": "ok", "status": "complete", "actions": []}
MOMOCHAT = {"assignments": {"*": {"engine": "momochat", "personas": ["p"]}}}


class LogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, overrides=None, responses=()):
        adapter = FakeAdapter(list(responses))
        core = await start_core(self.tmp, overrides or {}, adapter)
        self.cores.append(core)
        return core, adapter

    def book(self, core, address="chat:room", module="reminders", subject="alice"):
        return LogbookHandle(core.manager, address, module, subject)

    async def say(self, core, text, sender="alice", conversation="room", **options):
        chat = iface(core)
        await chat.say(text, sender=sender, conversation=conversation, **options)
        await settle(core)
        return chat.texts(conversation)[-1]

    async def test_lines_are_written_read_in_order_and_found_by_their_words(self):
        core, _ = await self.core()
        book = self.book(core)
        first = book.write("reminder", "Set a reminder  for the dentist", person=True, persona="p", data={"at": 5})
        book.write("landed", "飞机票已经改到周三", data={"session": "t-1"})
        self.assertEqual((first.module, first.kind, first.text, first.subject, first.persona, first.data),
                         ("reminders", "reminder", "Set a reminder for the dentist", "alice", "p", {"at": 5}))
        self.assertEqual([line.kind for line in book.recent()], ["reminder", "landed"])  # the older first
        self.assertEqual([line.kind for line in book.recent(kind="landed")], ["landed"])
        self.assertEqual([line.kind for line in book.find("the dentist")], ["reminder"])
        self.assertEqual([line.kind for line in book.find("改到哪天了")], ["landed"])
        self.assertEqual(book.find("the"), [])  # (only words too common to search by)
        self.assertEqual(self.book(core, "chat:other").recent(), [])  # filed by conversation

    async def test_a_line_must_make_sense_and_keeps_no_secret(self):
        core, _ = await self.core()
        book = self.book(core)
        for kind, text in (("", "x"), ("Two Words", "x"), ("note", "   "), (5, "x")):
            with self.assertRaises(ValueError):
                book.write(kind, text)
        with self.assertRaises(ValueError):
            book.write("note", "x", data={"big": "y" * 3000})
        secret = "sk-abcdefghijklmnopqrstuvwxyz"
        written = book.write("note", f"the key is {secret}", data={"key": secret})
        self.assertEqual((written.text, written.data), ("the key is [secret api key]", {"key": "[secret api key]"}))
        long = book.write("note", "z" * 900)
        self.assertEqual(len(long.text), 500)

    async def test_the_oldest_lines_go_past_the_limit(self):
        tank = Tank(self.tmp / "tank.db")
        self.addCleanup(tank.close)
        for index in range(5):
            tank.add_log("chat:room", "m", "note", f"line {index}", keep=3)
        self.assertEqual([line.text for line in tank.log_lines("chat:room")], ["line 4", "line 3", "line 2"])
        self.assertGreater(LINES_KEPT, 100)

    async def test_the_core_logs_a_write_that_ran_and_nothing_else(self):
        core, _ = await self.core({"owners": {"chat": ["boss"]}})
        await self.say(core, "$remember the blue folder", sender="boss")
        await self.say(core, "$notes", sender="boss")  # a read
        await self.say(core, "$remember", sender="boss")  # refused: nothing to remember
        lines = core.tank.log_lines("chat:room")
        self.assertEqual([(line.module, line.kind, line.subject) for line in lines], [("core", "command", "boss")])
        self.assertIn("basic.remember", lines[0].text)

    async def test_what_people_typed_is_in_their_log_but_never_shown_to_a_model(self):
        core, adapter = await self.core(MOMOCHAT, [ANSWER])
        self.book(core).write("action", "A thing she did")
        await self.say(core, "$remember the secret nickname", sender="boss")
        await self.say(core, "hello")
        context = turn_context(adapter.calls[-1])
        self.assertIn("A thing she did", context)
        self.assertNotIn("basic.remember", context)  # (a typed `$alias` is for one student, not all)

    async def test_her_turn_shows_the_last_lines_and_a_setting_turns_it_off(self):
        core, adapter = await self.core(MOMOCHAT, [ANSWER, ANSWER])
        book = self.book(core, module="reminders")
        book.write("note", "a note of someone, not for this")
        book.write("reminder", "Set a reminder for the dentist")
        await self.say(core, "hello")
        context = turn_context(adapter.calls[-1])
        self.assertIn("Done here lately", context)
        self.assertIn("Set a reminder for the dentist", context)
        self.assertNotIn("a note of someone", context)
        await core.stop()
        self.cores.remove(core)
        quiet = {"assignments": {"*": {"engine": "momochat", "personas": ["p"], "options": {"log_lines": 0}}}}
        core, adapter = await self.core(quiet, [ANSWER])
        await self.say(core, "hello again")
        self.assertNotIn("Done here lately", turn_context(adapter.calls[-1]))

    async def test_a_day_old_line_is_not_shown(self):
        core, adapter = await self.core(MOMOCHAT, [ANSWER])
        line = self.book(core).write("reminder", "Long ago")
        core.tank.db.execute("UPDATE log SET at=at-90000 WHERE id=?", (line.id,))
        await self.say(core, "hello")
        self.assertNotIn("Long ago", turn_context(adapter.calls[-1]))

    async def test_people_read_their_lines_and_admins_all_of_them_and_each_removes_what_they_see(self):
        core, _ = await self.core({"owners": {"chat": ["boss"]}})
        mine = self.book(core, subject="alice").write("note", "About Alice", person=True)
        self.book(core, subject="bob").write("note", "About Bob", person=True)
        everyone = self.book(core).write("landed", "A session landed")
        answer = await self.say(core, "$log")
        self.assertIn("About Alice", answer)
        self.assertNotIn("About Bob", answer)
        self.assertNotIn("A session landed", answer)
        admin = await self.say(core, "$log", sender="boss")
        for text in ("About Alice", "About Bob", "A session landed", "#"):
            self.assertIn(text, admin)
        self.assertIn("No line with that ID", await self.say(core, f"$log forget {everyone.id}"))
        self.assertIn("Line removed", await self.say(core, f"$log forget #{mine.id}"))
        self.assertIn("Nothing in this chat's log", await self.say(core, "$log"))
        self.assertIn("Removed 2", await self.say(core, "$log clear", sender="boss"))
        self.assertIn("$log forget", await self.say(core, "$log forget two"))
        self.assertIn("empty", await self.say(core, "$log 3", sender="boss"))

    async def test_memory_reads_and_removes_notes_about_you(self):
        core, _ = await self.core()
        tank = core.tank
        one = tank.add_log("chat:room", "core", "note", "Likes tea", subject="alice", persona="p")
        tank.add_log("chat:room", "core", "note", "Plays go", subject="alice", persona="p")
        tank.add_log("chat:room", "core", "note", "Bob's own", subject="bob", persona="p")
        tank.add_log("chat:room", "core", "action", "Not a note", subject="alice", persona="p")
        answer = await self.say(core, "$memory")
        self.assertIn("Likes tea", answer)
        self.assertNotIn("Bob's own", answer)
        self.assertNotIn("Not a note", answer)
        self.assertIn("Note removed", await self.say(core, f"$memory forget {one.id}"))
        self.assertIn("Removed 1", await self.say(core, "$memory clear"))
        self.assertEqual(tank.log_counts("chat:room", "bob", kind="note"), {"p": 1})
        self.assertEqual([line.kind for line in tank.log_lines("chat:room", subject="alice")], ["action"])

    def test_the_diarys_notes_carry_over_as_notes(self):
        path = self.tmp / "old.db"
        Tank(path).close()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY AUTOINCREMENT, persona TEXT NOT NULL, address TEXT NOT "
                       "NULL, subject TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL, importance "
                       "INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)")
            db.execute("INSERT INTO diary(persona,address,subject,key,kind,note,importance,created_at,updated_at) "
                       "VALUES('p','chat:room','alice','tea','fact','Likes tea',4,1,2)")
        tank = Tank(path)
        self.addCleanup(tank.close)
        line = tank.log_lines("chat:room")[0]
        self.assertEqual((line.kind, line.text, line.subject, line.persona, line.at), ("note", "Likes tea", "alice", "p", 2))
        self.assertEqual((line.module, line.data["key"], line.data["importance"]), ("core", "tea", 4))
        self.assertIsNone(tank.db.one("SELECT 1 FROM sqlite_master WHERE name='diary'"))

    def test_search_terms_take_words_and_pieces_of_chinese(self):
        self.assertEqual(search_terms("the dentist 改到周三"), ["dentist", "改到", "到周", "周三"])


if __name__ == "__main__":
    unittest.main()

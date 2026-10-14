from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.feature_memory import MemoryFeature
from services import database as db
from services.character_diary import diary_scope


class MemoryFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(db, "DATABASE_PATH", str(Path(directory.name) / "diary.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_database()
        self.feature = MemoryFeature()
        self.message = SimpleNamespace(content="$memory DeepSeek", channel_id="room", author_id="alice", metadata={})
        self.app = SimpleNamespace(config={})
        db.save_diary_note(diary_scope(self.message), "DeepSeek", "study.subject", "fact", "Alice studies physics.", 4)
        self.note_id = db.list_diary_notes(diary_scope(self.message), "DeepSeek")[0]["id"]

    async def test_owner_can_read_and_remove_note(self):
        response = await self.feature.handle(self.app, self.message)
        self.assertIn("Alice studies physics.", response[0].text)
        self.message.content = f"$memory forget DeepSeek {self.note_id}"
        response = await self.feature.handle(self.app, self.message)
        self.assertEqual(response[0].text, "Diary note removed.")
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    async def test_another_user_cannot_read_or_delete_an_owners_note(self):
        self.message.author_id = "bob"
        response = await self.feature.handle(self.app, self.message)
        self.assertNotIn("Alice studies physics.", response[0].text)
        self.message.content = f"$memory forget DeepSeek {self.note_id}"
        response = await self.feature.handle(self.app, self.message)
        self.assertIn("not found", response[0].text)
        self.assertEqual(len(db.list_diary_notes(("discord", "room", "alice"), "DeepSeek")), 1)

    async def test_clear_is_scoped_to_one_character_and_user(self):
        db.save_diary_note(diary_scope(self.message), "Luna", "study.subject", "fact", "Luna's own note.", 4)
        db.save_diary_note(("discord", "room", "bob"), "DeepSeek", "study.subject", "fact", "Bob's note.", 4)
        self.message.content = "$memory clear DeepSeek"
        await self.feature.handle(self.app, self.message)
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])
        self.assertEqual(len(db.list_diary_notes(diary_scope(self.message), "Luna")), 1)
        self.assertEqual(len(db.list_diary_notes(("discord", "room", "bob"), "DeepSeek")), 1)

    async def test_diary_dates_use_configured_user_timezone(self):
        with db.get_db() as conn:
            conn.execute("UPDATE character_diary SET updated_at='2026-10-14 01:00:00'")
        self.app.config["TIMEZONE"] = "America/Los_Angeles"
        response = await self.feature.handle(self.app, self.message)
        self.assertIn("2026-10-13 PDT", response[0].text)

    async def test_owner_can_inspect_older_notes_on_later_pages_without_other_users_data(self):
        for index in range(25):
            db.save_diary_note(diary_scope(self.message), "DeepSeek", f"moment.{index}", "episode", f"Alice's moment {index}.", 4)
            db.save_diary_note(("discord", "room", "bob"), "DeepSeek", f"moment.{index}", "episode", "Bob-only memory.", 5)
        self.message.content = "$memory DeepSeek 2"
        response = await self.feature.handle(self.app, self.message)
        self.assertIn("Page 2/2", response[0].text)
        self.assertIn("Alice studies physics.", response[0].text)
        self.assertNotIn("Bob-only memory.", response[0].text)


if __name__ == "__main__":
    unittest.main()

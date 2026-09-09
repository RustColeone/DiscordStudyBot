from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services import database as db


class ReminderManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = str(Path(self.directory.name) / "reminders.db")
        db.init_database()
        now = datetime.now(timezone.utc) + timedelta(hours=1)
        self.alice_id = db.create_reminder("channel", "alice", "Alice", "discord", now, "Alice task")
        self.bob_id = db.create_reminder("channel", "bob", "Bob", "discord", now, "Bob task")
        db.create_reminder("other", "alice", "Alice", "discord", now, "Other channel task")

    def tearDown(self):
        db.DATABASE_PATH = self.original_database_path
        self.directory.cleanup()

    def test_lists_only_requesting_users_channel_reminders(self):
        reminders = db.get_pending_reminders("channel", "alice")

        self.assertEqual([reminder["id"] for reminder in reminders], [self.alice_id])

    def test_user_cannot_delete_another_users_reminder(self):
        deleted = db.delete_pending_reminder(self.bob_id, "channel", "alice")

        self.assertFalse(deleted)
        self.assertEqual(len(db.get_pending_reminders("channel", "bob")), 1)

    def test_creator_can_delete_another_users_reminder_in_channel(self):
        deleted = db.delete_pending_reminder(
            self.bob_id,
            "channel",
            "creator",
            allow_any_author=True,
        )

        self.assertTrue(deleted)
        self.assertEqual(db.get_pending_reminders("channel", "bob"), [])


if __name__ == "__main__":
    unittest.main()

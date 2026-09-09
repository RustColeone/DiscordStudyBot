from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services import context_compression
from services import database as db


class ContextCompressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = str(Path(self.directory.name) / "context.db")
        db.init_database()

    def tearDown(self):
        db.DATABASE_PATH = self.original_database_path
        self.directory.cleanup()

    def test_compacts_old_turns_and_preserves_recent_context(self):
        db.save_chat_message("channel", "deepseek", "system", "main prompt")
        for index in range(8):
            db.save_chat_message("channel", "deepseek", "user", f"old request {index} " + "x" * 80)
            db.save_chat_message("channel", "deepseek", "assistant", f"old reply {index} " + "y" * 80)

        result = context_compression.compact_history_if_needed(
            "channel",
            "deepseek",
            lambda source: "durable summary",
            output_reserve=20,
            context_limit=300,
        )

        history = db.load_chat_history("channel", "deepseek")
        self.assertTrue(result.compressed)
        self.assertGreater(result.removed_messages, 0)
        self.assertEqual(history[0], {"role": "system", "content": "main prompt"})
        self.assertEqual(history[1]["role"], "system")
        self.assertIn("durable summary", history[1]["content"])
        self.assertIn("old reply 7", history[-1]["content"])

    def test_clear_removes_raw_and_compressed_history(self):
        db.save_chat_message("channel", "deepseek", "user", "old request " + "x" * 200)
        context_compression.compact_history_if_needed(
            "channel",
            "deepseek",
            lambda source: "summary",
            output_reserve=10,
            context_limit=50,
        )

        db.clear_chat_history("channel", "deepseek")

        self.assertEqual(db.load_chat_history("channel", "deepseek"), [])
        self.assertIsNone(db.get_chat_context_summary("channel", "deepseek"))

    def test_history_is_not_deleted_after_fifty_messages(self):
        for index in range(75):
            db.save_chat_message("channel", "deepseek", "user", f"message {index}")

        self.assertEqual(db.get_chat_history_count("channel", "deepseek"), 75)

    def test_forced_compaction_runs_below_normal_threshold(self):
        for index in range(6):
            db.save_chat_message("channel", "deepseek", "user", f"message {index}")

        result = context_compression.compact_history_if_needed(
            "channel",
            "deepseek",
            lambda source: "emergency summary",
            output_reserve=10,
            context_limit=10_000,
            force=True,
        )

        self.assertTrue(result.compressed)
        self.assertIn("emergency summary", db.get_chat_context_summary("channel", "deepseek"))


if __name__ == "__main__":
    unittest.main()
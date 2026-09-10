from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from services import database as db


class ModelDefaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = str(Path(self.directory.name) / "models.db")

    def tearDown(self):
        db.DATABASE_PATH = self.original_database_path
        self.directory.cleanup()

    def test_new_channels_default_to_deepseek_v41_flash(self):
        db.init_database()

        settings = db.get_channel_settings("new-channel")

        self.assertEqual(settings["llm"], "deepseek")
        self.assertEqual(settings["model"], "deepseek-flash")
        self.assertEqual(settings["effort"], "high")

    def test_retired_flash_alias_is_migrated(self):
        db.init_database()
        with sqlite3.connect(db.DATABASE_PATH) as connection:
            connection.execute(
                "INSERT INTO channel_settings (channel_id, active_llm, active_model) VALUES (?, ?, ?)",
                ("existing", "deepseek", "deepseek-v4-flash"),
            )
        db.init_database()

        self.assertEqual(db.get_channel_settings("existing")["model"], "deepseek-flash")


if __name__ == "__main__":
    unittest.main()
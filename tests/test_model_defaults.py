from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services import database as db
from providers import unified_chat


class ModelDefaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = str(Path(self.directory.name) / "models.db")

    def tearDown(self):
        db.DATABASE_PATH = self.original_database_path
        self.directory.cleanup()

    def test_new_channels_default_to_unified_deepseek_flash(self):
        db.init_database()

        settings = db.get_channel_settings("new-channel")

        self.assertEqual(settings["llm"], "deepseek")
        self.assertEqual(settings["model"], "deepseek-flash")
        self.assertEqual(settings["effort"], "high")
        self.assertEqual(settings["mode"], "agent")

    def test_chat_mode_can_switch_without_changing_roleplay_provider(self):
        db.init_database()
        db.set_channel_llm("channel", "gemini", "gemini-3.5-flash")

        db.set_chat_mode("channel", "roleplay")

        settings = db.get_channel_settings("channel")
        self.assertEqual(settings["mode"], "roleplay")
        self.assertEqual(settings["llm"], "gemini")
        self.assertEqual(settings["model"], "gemini-3.5-flash")

    def test_all_retired_deepseek_aliases_are_migrated(self):
        db.init_database()
        with sqlite3.connect(db.DATABASE_PATH) as connection:
            for index, model in enumerate((
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "deepseek-v4-pro",
                "deepseek-chat",
                "deepseek-reasoner",
            )):
                connection.execute(
                    "INSERT INTO channel_settings (channel_id, active_llm, active_model) VALUES (?, ?, ?)",
                    (f"existing-{index}", "deepseek", model),
                )
        db.init_database()

        for index in range(5):
            self.assertEqual(
                db.get_channel_settings(f"existing-{index}")["model"],
                "deepseek-flash",
            )

    def test_deepseek_discovery_exposes_only_unified_flash(self):
        client = Mock()
        client.models.list.return_value = SimpleNamespace(data=[
            SimpleNamespace(id="deepseek-chat"),
            SimpleNamespace(id="deepseek-flash"),
            SimpleNamespace(id="deepseek-reasoner"),
        ])

        with patch("providers.unified_chat.OpenAI", return_value=client), \
                patch("providers.unified_chat.optional_secret", return_value="configured"):
            models = unified_chat._fetch_deepseek_models()

        self.assertEqual(models, ["deepseek-flash"])

    def test_deepseek_has_provider_specific_multilingual_persona(self):
        prompt = unified_chat.get_default_prompt("deepseek")

        self.assertEqual(prompt["name"], "CETACEA_LOLI")
        self.assertNotIn("LANG_ZH_CN_ONLY", prompt["content"])
        self.assertIn("使用用户最近使用的语言回复", prompt["content"])

    def test_other_providers_keep_shared_default_persona(self):
        self.assertEqual(unified_chat.get_default_prompt("chatgpt")["name"], "Short Assistant")

    def test_agent_status_reports_independent_effort_values(self):
        settings = {"mode": "agent", "listen_mode": False}
        with patch.object(unified_chat.db, "get_channel_settings", return_value=settings), \
                patch.object(unified_chat.collaborative_agent, "AGENT_DEEPSEEK_EFFORT", "low"), \
                patch.object(unified_chat.collaborative_agent, "AGENT_LUNA_EFFORT", "medium"), \
                patch.object(unified_chat.collaborative_agent, "AGENT_GEMINI_EFFORT", "high"), \
                patch.object(unified_chat.collaborative_agent, "AGENT_CLAUDE_EFFORT", "low"):
            status = unified_chat.get_status("channel")

        self.assertIn("DeepSeek effort: **low**", status)
        self.assertIn("Luna effort: **medium**", status)
        self.assertIn("Gemini effort: **high**", status)
        self.assertIn("Claude effort: **low**", status)


if __name__ == "__main__":
    unittest.main()
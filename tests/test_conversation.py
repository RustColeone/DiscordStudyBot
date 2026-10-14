import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import database as db
from services.conversation import conversation_context, room_invitees


class ConversationContextTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(db, "DATABASE_PATH", str(Path(directory.name) / "history.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_database()

    def test_recent_context_keeps_speaker_order_and_isolates_rooms_and_modes(self):
        db.save_chat_message("room", "agent", "system", "Private character instructions")
        for index in range(15):
            db.save_chat_message("room", "agent", "user", f"[User]: turn {index}")
        db.save_chat_message("other-room", "agent", "user", "Other room")
        db.save_chat_message("room", "deepseek", "user", "Separate roleplay")

        context = conversation_context("room", "agent")
        payload = json.loads(context.split("\n", 1)[1])
        self.assertEqual(payload["recent_turns"][0]["content"], "[User]: turn 3")
        self.assertEqual(payload["recent_turns"][-1]["content"], "[User]: turn 14")
        for excluded in ("Private character instructions", "Other room", "Separate roleplay"):
            self.assertNotIn(excluded, context)

    def test_long_history_retains_latest_answer_and_durable_summary_within_budget(self):
        for index in range(12):
            db.save_chat_message("room", "agent", "assistant", str(index) + "字" * 5000)
        db.save_chat_message("room", "agent", "user", "[User]: Starting is the hard part.")
        with patch.object(db, "get_chat_context_summary", return_value="User studies physics. " * 300):
            context = conversation_context("room", "agent")
        payload = json.loads(context.split("\n", 1)[1])
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), 12000)
        self.assertIn("User studies physics.", payload["summary"])
        self.assertEqual(payload["recent_turns"][-1]["content"], "[User]: Starting is the hard part.")

    def test_empty_room_has_no_invented_context(self):
        self.assertEqual(conversation_context("new-room", "agent"), "")

    def test_plural_address_invites_room_in_chinese_and_english(self):
        for request in ("烧卖你们觉得好吃吗？这个是我做的", "你們覺得呢？", "What do you all think?", "大家早安"):
            with self.subTest(request=request):
                self.assertEqual(room_invitees(request), ["Luna", "Gemini", "Claude"])

    def test_quoted_plural_and_selective_request_do_not_invite_everyone(self):
        self.assertEqual(room_invitees('“你们”是什么意思？'), [])
        self.assertEqual(room_invitees("你们不用都回答，Luna学姐说说就行"), ["Luna"])
        self.assertEqual(room_invitees("Haiku学姐，你怎么看？"), ["Claude"])

    def test_long_turn_keeps_speaker_identity_and_latest_words(self):
        db.save_chat_message("room", "agent", "user", "[Alice]: " + "text " * 1000 + "I prefer physics.")
        payload = json.loads(conversation_context("room", "agent").split("\n", 1)[1])
        content = payload["recent_turns"][0]["content"]
        self.assertTrue(content.startswith("[Alice]: "))
        self.assertTrue(content.endswith("I prefer physics."))
        self.assertLessEqual(len(content), 2000)


if __name__ == "__main__":
    unittest.main()

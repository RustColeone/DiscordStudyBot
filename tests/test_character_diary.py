import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import database as db
from services.character_diary import consume_diary_updates, diary_context, diary_scope, CONTEXT_BUDGET


class CharacterDiaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(db, "DATABASE_PATH", str(Path(directory.name) / "memory.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_database()
        self.message = SimpleNamespace(
            content="I'm studying Cantonese.", channel_id="room", author_id="alice",
            author_display_name="Alice", metadata={"platform": "discord"},
        )

    @staticmethod
    def update(key="study.language", note="Alice is learning Cantonese; I'd like to encourage her.", importance=4):
        return {"key": key, "kind": "fact", "note": note, "importance": importance}

    @staticmethod
    def block(updates, spoken="How's that going?"):
        return spoken + "\n<diary_updates>" + json.dumps(updates) + "</diary_updates>"

    def test_note_is_selective_and_hidden_without_archiving_the_exchange(self):
        visible = consume_diary_updates(self.message, "DeepSeek", self.block([self.update()]))
        self.assertEqual(visible, "How's that going?")
        notes = db.list_diary_notes(diary_scope(self.message), "DeepSeek")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["note"], self.update()["note"])
        self.assertNotIn(self.message.content, notes[0]["note"])
        self.assertEqual(db.load_chat_history("room", "agent"), [])

    def test_each_character_owns_her_notes_and_receives_only_her_diary(self):
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update(note="I'm excited about Alice's Cantonese practice.")]))
        consume_diary_updates(self.message, "Luna", self.block([self.update(note="I should support Alice's study routine gently.")]))
        deepseek = diary_context(self.message, "DeepSeek")
        luna = diary_context(self.message, "Luna")
        self.assertIn("I'm excited", deepseek)
        self.assertNotIn("routine gently", deepseek)
        self.assertIn("routine gently", luna)
        self.assertNotIn("I'm excited", luna)

    def test_diaries_are_scoped_by_user_room_and_platform(self):
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update(note="Only Alice's note.")]))
        for field, value in (("author_id", "bob"), ("channel_id", "other-room"), ("metadata", {"platform": "wechat"})):
            message = SimpleNamespace(**{**vars(self.message), field: value})
            with self.subTest(field=field):
                self.assertNotIn("Only Alice's note.", diary_context(message, "DeepSeek"))

    def test_correction_replaces_same_key_instead_of_accumulating_contradictions(self):
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update(note="Alice studies Cantonese.")]))
        self.message.content = "I've switched to Japanese."
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update(note="Alice now studies Japanese.")]))
        notes = db.list_diary_notes(diary_scope(self.message), "DeepSeek")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["note"], "Alice now studies Japanese.")
        self.assertNotIn("studies Cantonese", diary_context(self.message, "DeepSeek"))

    def test_repeated_note_does_not_refresh_or_duplicate_it(self):
        block = self.block([self.update()])
        consume_diary_updates(self.message, "DeepSeek", block)
        with db.get_db() as conn:
            conn.execute("UPDATE character_diary SET updated_at='2000-01-01 00:00:00'")
        consume_diary_updates(self.message, "DeepSeek", block)
        notes = db.list_diary_notes(diary_scope(self.message), "DeepSeek")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["updated_at"], "2000-01-01 00:00:00")

    def test_low_interest_turn_and_plain_reply_store_nothing(self):
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update(importance=2)]))
        self.assertEqual(consume_diary_updates(self.message, "DeepSeek", "Hello!"), "Hello!")
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    def test_verbatim_message_is_not_accepted_as_a_diary_note(self):
        visible = consume_diary_updates(self.message, "DeepSeek", self.block([self.update(note=self.message.content)]))
        self.assertEqual(visible, "How's that going?")
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    def test_invalid_batch_is_hidden_and_does_not_partially_save(self):
        invalid = [self.update(), {**self.update(key="other"), "character": "Luna"}]
        self.assertEqual(consume_diary_updates(self.message, "DeepSeek", self.block(invalid)), "How's that going?")
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    def test_clipped_or_malformed_block_never_leaks_diary_data(self):
        for block in ('<diary_updates>[{"note":"private', '<diary_updates>invalid</diary_updates>'):
            with self.subTest(block=block):
                self.assertEqual(consume_diary_updates(self.message, "DeepSeek", "Hello!\n" + block), "Hello!")
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    def test_unknown_writer_cannot_create_notes(self):
        consume_diary_updates(self.message, "Unknown", self.block([self.update()]))
        self.assertEqual(db.list_diary_notes(diary_scope(self.message), "DeepSeek"), [])

    def test_retrieval_brings_back_relevant_old_moment_within_context_budget(self):
        scope = diary_scope(self.message)
        db.save_diary_note(scope, "DeepSeek", "cooking.shaomai", "episode", "Alice shared homemade 烧卖 with us.", 3)
        for index in range(20):
            db.save_diary_note(scope, "DeepSeek", f"other.{index}", "fact", "Other important preference. " + "字" * 350, 5)
        self.message.content = "你还记得我做的烧卖吗？"
        context = diary_context(self.message, "DeepSeek")
        self.assertIn("homemade 烧卖", context)
        data = context.split("\n", 2)[1]
        self.assertLessEqual(len(data), CONTEXT_BUDGET)

    def test_storage_survives_database_reinitialization(self):
        consume_diary_updates(self.message, "DeepSeek", self.block([self.update()]))
        db.init_database()
        self.assertEqual(len(db.list_diary_notes(diary_scope(self.message), "DeepSeek")), 1)

    def test_storage_failure_does_not_break_or_expose_the_reply(self):
        with patch.object(db, "save_diary_note", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(consume_diary_updates(self.message, "DeepSeek", self.block([self.update()])), "How's that going?")


if __name__ == "__main__":
    unittest.main()

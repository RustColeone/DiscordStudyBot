import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services import attachment_store
from services import database as db


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.headers = {"Content-Length": str(len(body))}
        self.closed = False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        return iter([self.body])

    def close(self):
        self.closed = True


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.database_path = root / "test.db"
        self.cache_root = root / "cache"
        self.database_patch = patch.object(db, "DATABASE_PATH", str(self.database_path))
        self.cache_patch = patch.object(attachment_store, "CACHE_ROOT", self.cache_root)
        self.database_patch.start()
        self.cache_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.cache_patch.stop)
        db.init_database()

    def _save(self, filename="notes.txt", content_type="text/plain", size=5, url=None):
        ids = db.save_conversation_attachments("channel", [{
            "message_id": "100",
            "attachment_id": "200",
            "author_id": "user",
            "filename": filename,
            "url": url or f"https://cdn.discordapp.com/{filename}",
            "content_type": content_type,
            "size": size,
            "created_at": "2026-09-11T00:00:00+00:00",
        }])
        return db.find_conversation_attachment("channel", str(ids[0]))

    def test_metadata_survives_independently_of_chat_history(self):
        record = self._save()

        db.save_chat_message("channel", "agent", "user", "hello")
        db.clear_chat_history("channel", "agent")

        loaded = db.find_conversation_attachment("channel", str(record["id"]))
        self.assertEqual(loaded["filename"], "notes.txt")
        self.assertEqual(loaded["attachment_id"], "200")

    def test_refreshed_discord_url_updates_existing_attachment(self):
        first = self._save()

        second = self._save(url="https://cdn.discordapp.com/notes.txt?renewed=1")

        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["url"], "https://cdn.discordapp.com/notes.txt?renewed=1")
        self.assertEqual(len(db.list_conversation_attachments("channel")), 1)

    def test_text_is_downloaded_lazily_and_reused_from_cache(self):
        record = self._save(size=11)
        response = FakeResponse(b"hello world")

        with patch.object(attachment_store.requests, "get", return_value=response) as get:
            first = attachment_store.read_attachment(record)
            second = attachment_store.read_attachment(record)

        self.assertEqual(first["text"], "hello world")
        self.assertEqual(second["text"], "hello world")
        get.assert_called_once()
        self.assertTrue(Path(first["cached_path"]).is_file())
        self.assertTrue(response.closed)

    def test_image_result_uses_cached_local_path(self):
        record = self._save("photo.png", "image/png", 5)
        with patch.object(
            attachment_store.requests, "get", return_value=FakeResponse(b"image")
        ):
            result = attachment_store.read_attachment(record)

        self.assertEqual(result["image"]["path"], result["cached_path"])
        self.assertEqual(result["image"]["content_type"], "image/png")

    def test_oldest_cached_file_is_evicted_to_make_room(self):
        first = self._save("first.txt", size=6)
        second = self._save(
            "second.txt", size=6, url="https://cdn.discordapp.com/second-v2.txt"
        )
        responses = [FakeResponse(b"123456"), FakeResponse(b"abcdef")]

        with patch.object(attachment_store, "CONVERSATION_CACHE_BYTES", 10), \
                patch.object(attachment_store.requests, "get", side_effect=responses):
            first_result = attachment_store.read_attachment(first)
            second_result = attachment_store.read_attachment(second)

        self.assertFalse(Path(first_result["cached_path"]).exists())
        self.assertTrue(Path(second_result["cached_path"]).exists())

    def test_stale_cached_file_is_deleted(self):
        record = self._save()
        with patch.object(
            attachment_store.requests, "get", return_value=FakeResponse(b"hello")
        ):
            result = attachment_store.read_attachment(record)
        stale_time = 1_000.0
        os.utime(result["cached_path"], (stale_time, stale_time))

        deleted = attachment_store.cleanup_expired_cache(
            stale_time + attachment_store.CACHE_TTL_SECONDS + 1
        )

        self.assertEqual(deleted, 1)
        self.assertFalse(Path(result["cached_path"]).exists())


if __name__ == "__main__":
    unittest.main()
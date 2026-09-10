from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.vision import download_image_bytes, inline_image_urls


class FakeResponse:
    def __init__(self, chunks, content_length=None):
        self.chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        return iter(self.chunks)


class VisionTests(unittest.TestCase):
    def test_downloads_image_and_builds_inline_data_url(self):
        image = {
            "url": "https://cdn.discordapp.com/photo.png",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 5,
        }
        with patch(
            "services.vision.requests.get",
            return_value=FakeResponse([b"image"], content_length=5),
        ) as get:
            inline = inline_image_urls([image])

        get.assert_called_once_with(image["url"], stream=True, timeout=(10, 60))
        self.assertEqual(inline[0]["url"], "data:image/png;base64,aW1hZ2U=")

    def test_rejects_oversized_image_from_content_length(self):
        with patch(
            "services.vision.requests.get",
            return_value=FakeResponse([], content_length=11),
        ):
            with self.assertRaisesRegex(ValueError, "32 MiB"):
                download_image_bytes("https://cdn.discordapp.com/photo.png", max_bytes=10)

    def test_rejects_stream_that_grows_past_limit(self):
        with patch(
            "services.vision.requests.get",
            return_value=FakeResponse([b"12345", b"67890", b"x"]),
        ):
            with self.assertRaisesRegex(ValueError, "32 MiB"):
                download_image_bytes("https://cdn.discordapp.com/photo.png", max_bytes=10)


if __name__ == "__main__":
    unittest.main()
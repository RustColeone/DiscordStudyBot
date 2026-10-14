from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from providers import image_search


class ImageSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.output_patch = patch.object(
            image_search,
            "OUTPUT_DIR",
            Path(self.temporary_directory.name),
        )
        self.output_patch.start()
        self.addCleanup(self.output_patch.stop)

    @staticmethod
    def _png_bytes() -> bytes:
        stream = BytesIO()
        Image.new("RGB", (32, 24), "white").save(stream, format="PNG")
        return stream.getvalue()

    def test_locates_and_normalizes_attributed_image(self):
        ddgs = Mock()
        ddgs.images.return_value = [{
            "title": "Buck converter diagram",
            "image": "https://images.example/buck.jpg",
            "url": "https://source.example/article",
        }]

        with patch.object(image_search, "DDGS", return_value=ddgs), \
                patch.object(image_search, "download_image_bytes", return_value=self._png_bytes()):
            result = image_search.locate_image("buck converter operation diagram")

        self.addCleanup(lambda: Path(result.path).unlink(missing_ok=True))
        self.assertEqual(result.mime_type, "image/png")
        self.assertEqual(result.title, "Buck converter diagram")
        self.assertEqual(result.source_url, "https://source.example/article")
        self.assertTrue(Path(result.path).is_file())
        ddgs.images.assert_called_once_with(
            "buck converter operation diagram",
            max_results=8,
            safesearch="moderate",
        )

    def test_skips_an_invalid_candidate(self):
        ddgs = Mock()
        ddgs.images.return_value = [
            {"image": "https://images.example/bad.jpg"},
            {"image": "https://images.example/good.jpg"},
        ]

        with patch.object(image_search, "DDGS", return_value=ddgs), \
                patch.object(
                    image_search,
                    "download_image_bytes",
                    side_effect=[b"not an image", self._png_bytes()],
                ):
            result = image_search.locate_image("diagram")

        self.addCleanup(lambda: Path(result.path).unlink(missing_ok=True))
        self.assertEqual(result.image_url, "https://images.example/good.jpg")


if __name__ == "__main__":
    unittest.main()
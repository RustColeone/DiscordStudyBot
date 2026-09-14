from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google.genai import types

from providers import gemini_query, unified_chat


class GeminiQueryTests(unittest.TestCase):
    def test_query_uses_google_genai_and_persists_exchange(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="Gemini reply")

        with patch.object(gemini_query, "client", client), \
                patch.object(gemini_query, "compact_history_if_needed"), \
                patch.object(gemini_query, "_get_current_prompt_index", return_value=0), \
                patch.object(gemini_query, "_build_request", return_value=("persona", [])), \
                patch.object(gemini_query.db, "save_chat_message") as save_message:
            answer = gemini_query.queryGemini(
                "hello", "channel", None, model="gemini-3.5-flash-lite"
            )

        self.assertEqual(answer, "Gemini reply")
        request = client.models.generate_content.call_args.kwargs
        self.assertEqual(request["model"], "gemini-3.5-flash-lite")
        self.assertEqual(request["config"].system_instruction, "persona")
        self.assertIsInstance(request["contents"][-1], types.Content)
        self.assertEqual(save_message.call_count, 2)

    def test_query_builds_inline_image_with_new_sdk(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="I see it")
        image = {"url": "https://example.com/image.png", "content_type": "image/png"}

        with patch.object(gemini_query, "client", client), \
                patch.object(gemini_query, "compact_history_if_needed"), \
                patch.object(gemini_query, "_get_current_prompt_index", return_value=0), \
                patch.object(gemini_query, "_build_request", return_value=("persona", [])), \
                patch.object(gemini_query, "download_image_bytes", return_value=b"png"), \
                patch.object(gemini_query.db, "save_chat_message"):
            answer = gemini_query.queryGemini(
                "describe", "channel", None, images=[image]
            )

        self.assertEqual(answer, "I see it")
        parts = client.models.generate_content.call_args.kwargs["contents"][-1].parts
        self.assertEqual(parts[1].inline_data.mime_type, "image/png")
        self.assertEqual(parts[1].inline_data.data, b"png")

    def test_model_discovery_uses_supported_actions(self):
        client = Mock()
        client.models.list.return_value = [SimpleNamespace(
            name="models/gemini-3.5-flash-lite",
            supported_actions=["generateContent"],
        )]

        with patch.dict("os.environ", {"GEMINI_API_KEY": "configured"}), \
                patch.object(unified_chat.genai, "Client", return_value=client):
            models = unified_chat._fetch_gemini_models()

        self.assertEqual(models, ["gemini-3.5-flash-lite"])


if __name__ == "__main__":
    unittest.main()
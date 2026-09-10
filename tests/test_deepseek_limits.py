from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import deepseek_query


class DeepSeekLimitTests(unittest.TestCase):
    def test_high_effort_uses_supported_maximum_output(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="done"),
            )]
        )

        with patch.object(deepseek_query, "client", client), \
                patch.object(deepseek_query, "_load_history_from_db", return_value=[]), \
                patch.object(deepseek_query.db, "save_chat_message"):
            result = deepseek_query.queryDeepSeek("hello", "channel", None, effort="high")

        self.assertEqual(result, "done")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["max_tokens"], 384000)

    def test_output_limit_is_reported(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="length",
                message=SimpleNamespace(content="partial"),
            )]
        )

        with patch.object(deepseek_query, "client", client), \
                patch.object(deepseek_query, "_load_history_from_db", return_value=[]), \
                patch.object(deepseek_query.db, "save_chat_message"):
            result = deepseek_query.queryDeepSeek("hello", "channel", None, effort="high")

        self.assertIn("partial", result)
        self.assertIn("Ask me to continue", result)

    def test_image_is_sent_as_multimodal_user_content(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="a cat"),
            )]
        )
        images = [{
            "url": "https://cdn.discordapp.com/photo.png",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
        }]

        with patch.object(deepseek_query, "client", client), \
                patch.object(deepseek_query, "_load_history_from_db", return_value=[]), \
                patch.object(deepseek_query, "inline_image_urls", return_value=[{
                    **images[0],
                    "url": "data:image/png;base64,aW1hZ2U=",
                }]), \
                patch.object(deepseek_query.db, "save_chat_message") as save_message:
            result = deepseek_query.queryDeepSeek(
                "what is this?", "channel", None, images=images
            )

        self.assertEqual(result, "a cat")
        user_content = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertEqual(user_content[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(
            user_content[1]["image_url"]["url"],
            "data:image/png;base64,aW1hZ2U=",
        )
        self.assertIn("[Attached images: photo.png]", save_message.call_args_list[0].args[3])

    def test_image_download_failure_does_not_call_deepseek_or_persist(self):
        client = Mock()
        images = [{
            "url": "https://cdn.discordapp.com/photo.png",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
        }]

        with patch.object(deepseek_query, "client", client), \
                patch.object(deepseek_query, "_load_history_from_db", return_value=[]), \
                patch.object(deepseek_query, "inline_image_urls", side_effect=RuntimeError("HTTP 403")), \
                patch.object(deepseek_query.db, "save_chat_message") as save_message:
            result = deepseek_query.queryDeepSeek(
                "what is this?", "channel", None, images=images
            )

        self.assertEqual(result, "DeepSeek image preparation failed: HTTP 403")
        client.chat.completions.create.assert_not_called()
        save_message.assert_not_called()

    def test_context_overflow_forces_compression_and_retries(self):
        client = Mock()
        client.chat.completions.create.side_effect = [
            Exception("maximum context length exceeded"),
            SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="recovered"),
            )]),
        ]
        compression = SimpleNamespace(compressed=True)

        with patch.object(deepseek_query, "client", client), \
                patch.object(deepseek_query, "_load_history_from_db", return_value=[]), \
                patch.object(deepseek_query, "compact_history_if_needed", return_value=compression) as compact, \
                patch.object(deepseek_query.db, "load_chat_history", return_value=[]), \
                patch.object(deepseek_query.db, "save_chat_message"):
            result = deepseek_query.queryDeepSeek("hello", "channel", None, effort="high")

        self.assertEqual(result, "recovered")
        self.assertTrue(compact.call_args.kwargs["force"])
        self.assertEqual(client.chat.completions.create.call_count, 2)


if __name__ == "__main__":
    unittest.main()
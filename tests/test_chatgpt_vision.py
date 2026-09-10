from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import chatgpt_query


class ChatGPTVisionTests(unittest.TestCase):
    def test_image_is_downloaded_locally_and_sent_inline(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="a car"))]
        )
        image = {
            "url": "https://cdn.discordapp.com/photo.jpg",
            "filename": "photo.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
        }
        inline_image = {**image, "url": "data:image/jpeg;base64,aW1hZ2U="}

        with patch.object(chatgpt_query, "client", client), \
                patch.object(chatgpt_query, "_load_history_from_db", return_value=[]), \
                patch.object(chatgpt_query, "inline_image_urls", return_value=[inline_image]) as localize, \
                patch.object(chatgpt_query.db, "save_chat_message"):
            result = chatgpt_query.queryChatGPT(
                "what is this?", "channel", None, model="gpt-4o", images=[image]
            )

        self.assertEqual(result, "a car")
        localize.assert_called_once_with([image])
        content = client.chat.completions.create.call_args.kwargs["messages"][-1]["content"]
        self.assertEqual(content[1]["image_url"]["url"], inline_image["url"])

    def test_local_download_failure_does_not_call_openai(self):
        client = Mock()
        with patch.object(chatgpt_query, "client", client), \
                patch.object(chatgpt_query, "_load_history_from_db", return_value=[]), \
                patch.object(chatgpt_query, "inline_image_urls", side_effect=RuntimeError("HTTP 403")), \
                patch.object(chatgpt_query.db, "save_chat_message"):
            result = chatgpt_query.queryChatGPT(
                "what is this?",
                "channel",
                None,
                model="gpt-4o",
                images=[{"url": "https://cdn.discordapp.com/photo.jpg"}],
            )

        self.assertEqual(result, "ChatGPT image preparation failed: HTTP 403")
        client.chat.completions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
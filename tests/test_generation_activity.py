from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.bot_types import IncomingMessage
from features.feature_chat import ChatFeature
from services.generation_activity import show_generation_typing


class TypingContext:
    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True

    async def __aexit__(self, exc_type, exc, traceback):
        self.exited = True


class GenerationActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_discord_typing_only_inside_generation_scope(self):
        context = TypingContext()
        channel = SimpleNamespace(typing=lambda: context)
        message = SimpleNamespace(metadata={"native_channel": channel})

        self.assertFalse(context.entered)
        async with show_generation_typing(message):
            self.assertTrue(context.entered)
            self.assertFalse(context.exited)
        self.assertTrue(context.exited)

    async def test_non_discord_messages_are_supported(self):
        message = SimpleNamespace(metadata={})

        async with show_generation_typing(message):
            pass

    async def test_passive_message_does_not_type_when_listen_mode_is_off(self):
        context = TypingContext()
        channel = SimpleNamespace(typing=lambda: context)
        message = IncomingMessage(
            content="ordinary message",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=None,
            metadata={"native_channel": channel},
        )

        with patch(
            "features.feature_chat.db.get_channel_settings",
            return_value={"listen_mode": False},
        ), patch("features.feature_chat.unified_chat.query_chat") as query_chat:
            responses = await ChatFeature().handle_passive(SimpleNamespace(config={}), message)

        self.assertEqual(responses, [])
        self.assertFalse(context.entered)
        query_chat.assert_not_called()

    async def test_passive_generation_types_only_while_model_runs(self):
        context = TypingContext()
        channel = SimpleNamespace(typing=lambda: context)
        message = IncomingMessage(
            content="ordinary message",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=None,
            metadata={"native_channel": channel},
        )

        with patch(
            "features.feature_chat.db.get_channel_settings",
            return_value={"listen_mode": True},
        ), patch("features.feature_chat.unified_chat.query_chat", return_value="reply"):
            responses = await ChatFeature().handle_passive(SimpleNamespace(config={}), message)

        self.assertEqual(responses[0].text, "reply")
        self.assertTrue(context.entered)
        self.assertTrue(context.exited)


if __name__ == "__main__":
    unittest.main()
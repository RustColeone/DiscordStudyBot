from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.bot_types import BotResponse, IncomingMessage
from features.feature_chat import ChatFeature


class ChatRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.feature = ChatFeature()
        self.agent = SimpleNamespace(handle=AsyncMock(return_value=[BotResponse(text="reminder scheduled")]))
        self.app = SimpleNamespace(
            agent_feature=self.agent,
            config={"CREATOR_PROMPT": "private creator context"},
        )

    def message(self, content, is_creator=True):
        return IncomingMessage(
            content=content,
            channel_id="channel",
            author_id="creator",
            author_name="comfortablynumb01",
            author_display_name="Laplace",
            created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            metadata={"is_creator": is_creator},
        )

    async def test_chat_reminder_is_routed_to_agent(self):
        message = self.message("$chat -s remind me tomorrow at 1900 to watch tv together")

        with patch("features.feature_chat.unified_chat.query_chat") as query_chat:
            responses = await self.feature.handle(self.app, message)

        query_chat.assert_not_called()
        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent remind me tomorrow at 1900 to watch tv together")
        self.assertEqual(responses[0].text, "reminder scheduled")

    async def test_creator_chat_receives_private_context(self):
        message = self.message("$chat -s hello")

        with patch("features.feature_chat.unified_chat.query_chat", return_value="hello") as query_chat:
            await self.feature.handle(self.app, message)

        self.assertEqual(query_chat.call_args.args[4], "private creator context")

    async def test_other_users_do_not_receive_private_context(self):
        message = self.message("$chat -s hello", is_creator=False)

        with patch("features.feature_chat.unified_chat.query_chat", return_value="hello") as query_chat:
            await self.feature.handle(self.app, message)

        self.assertIsNone(query_chat.call_args.args[4])


if __name__ == "__main__":
    unittest.main()
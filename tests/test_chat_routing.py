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

    async def test_chinese_chat_reminder_is_routed_to_agent(self):
        message = self.message("$chat -s 明天19点提醒我们看射雕英雄传")

        with patch("features.feature_chat.unified_chat.query_chat") as query_chat:
            responses = await self.feature.handle(self.app, message)

        query_chat.assert_not_called()
        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent 明天19点提醒我们看射雕英雄传")
        self.assertEqual(responses[0].text, "reminder scheduled")

    async def test_chinese_status_request_is_routed_to_agent(self):
        message = self.message("$chat -s 显示服务器状态")

        with patch("features.feature_chat.unified_chat.query_chat") as query_chat:
            await self.feature.handle(self.app, message)

        query_chat.assert_not_called()
        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent 显示服务器状态")

    async def test_effort_setting_is_persisted(self):
        message = self.message("$chat --effort high")

        with patch(
            "features.feature_chat.unified_chat.set_effort",
            return_value="Effort set to **high** for this channel",
        ) as set_effort:
            responses = await self.feature.handle(self.app, message)

        set_effort.assert_called_once_with("channel", "high")
        self.assertIn("Effort set to **high**", responses[0].text)

    async def test_ordinary_creator_chat_is_routed_to_agent(self):
        message = self.message("$chat -s hello")

        await self.feature.handle(self.app, message)

        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent hello")
        self.assertTrue(routed_message.metadata["is_creator"])

    async def test_ordinary_user_chat_is_routed_to_agent(self):
        message = self.message("$chat -s hello", is_creator=False)

        await self.feature.handle(self.app, message)

        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent hello")
        self.assertFalse(routed_message.metadata["is_creator"])


if __name__ == "__main__":
    unittest.main()
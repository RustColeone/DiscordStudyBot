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

        with patch("features.feature_chat.db.get_channel_settings", return_value={"mode": "roleplay"}), patch(
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

    async def test_trace_flag_is_forwarded_to_agent_metadata(self):
        message = self.message('$chat --trace -s "research this"')

        await self.feature.handle(self.app, message)

        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent research this")
        self.assertTrue(routed_message.metadata["show_trace"])

    async def test_trace_image_metadata_is_forwarded_to_agent(self):
        message = self.message('$chat --trace -s "what car is this?"')
        image = {
            "url": "https://cdn.discordapp.com/car.jpg",
            "filename": "car.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
        }
        message.metadata["image_attachments"] = [image]

        await self.feature.handle(self.app, message)

        routed_message = self.agent.handle.await_args.args[1]
        self.assertEqual(routed_message.content, "$agent what car is this?")
        self.assertEqual(routed_message.metadata["image_attachments"], [image])
        self.assertTrue(routed_message.metadata["show_trace"])

    async def test_non_creator_can_change_mode(self):
        message = self.message("$chat --mode roleplay", is_creator=False)

        with patch("features.feature_chat.db.set_chat_mode") as set_mode:
            responses = await self.feature.handle(self.app, message)

        set_mode.assert_called_once_with("channel", "roleplay")
        self.assertIn("Chat mode set to **roleplay**", responses[0].text)

    async def test_non_creator_can_change_roleplay_model(self):
        message = self.message("$chat --mode roleplay --model gpt-4o", is_creator=False)

        with patch("features.feature_chat.db.set_chat_mode") as set_mode, \
                patch("features.feature_chat.unified_chat.set_model", return_value="model changed") as set_model:
            responses = await self.feature.handle(self.app, message)

        set_mode.assert_called_once_with("channel", "roleplay")
        set_model.assert_called_once_with("channel", "gpt-4o")
        self.assertIn("model changed", responses[0].text)

    async def test_agent_configuration_is_server_admin_only(self):
        message = self.message("$chat --model another-model", is_creator=False)

        with patch(
            "features.feature_chat.db.get_channel_settings",
            return_value={"mode": "agent"},
        ), patch("features.feature_chat.unified_chat.set_model") as set_model:
            responses = await self.feature.handle(self.app, message)

        self.assertIn("server-admin configured", responses[0].text)
        set_model.assert_not_called()

    async def test_creator_can_switch_to_roleplay_then_set_provider(self):
        message = self.message("$chat --mode roleplay --llm gemini")

        with patch("features.feature_chat.db.set_chat_mode") as set_mode, \
                patch("features.feature_chat.unified_chat.set_llm", return_value="switched") as set_llm:
            responses = await self.feature.handle(self.app, message)

        set_mode.assert_called_once_with("channel", "roleplay")
        set_llm.assert_called_once_with("channel", "gemini")
        self.assertIn("Chat mode set to **roleplay**", responses[0].text)

    async def test_agent_response_metadata_is_preserved(self):
        self.agent.handle.return_value = [BotResponse(
            text="grounded answer",
            metadata={"suppress_embeds": True},
        )]

        responses = await self.feature.handle(self.app, self.message("$chat -s current news"))

        self.assertTrue(responses[0].metadata["suppress_embeds"])

    async def test_passive_agent_mode_uses_typed_agent(self):
        message = self.message("what is current?", is_creator=False)
        with patch(
            "features.feature_chat.db.get_channel_settings",
            return_value={"listen_mode": True, "mode": "agent"},
        ):
            responses = await self.feature.handle_passive(self.app, message)

        routed = self.agent.handle.await_args.args[1]
        self.assertEqual(routed.content, "$agent what is current?")
        self.assertEqual(responses[0].text, "reminder scheduled")


if __name__ == "__main__":
    unittest.main()
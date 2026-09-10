from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.bot_types import BotAttachment, BotResponse, IncomingMessage
from features.feature_agent import AgentFeature


class StubFeature:
    def __init__(self, response=None):
        self.response = response
        self.messages = []

    async def handle(self, app, message):
        self.messages.append(message)
        return self.response or [BotResponse(text=message.content)]


class StubTimeFeature(StubFeature):
    def create_natural_reminder(self, app, message, text):
        return [BotResponse(text=f"scheduled: {text}")]

    def list_reminders(self, message, include_all=False):
        return "pending reminders"

    def delete_reminder(self, message, reminder_id):
        return f"deleted reminder {reminder_id}"


class StubProgressPlatform:
    supports_progress = True

    def __init__(self):
        self.sent = []
        self.edited = []
        self.deleted = []

    async def send_channel_message(self, channel_id, content):
        progress = object()
        self.sent.append((channel_id, content, progress))
        return progress

    async def edit_message(self, message, content):
        self.edited.append((message, content))

    async def delete_message(self, message):
        self.deleted.append(message)


class AgentFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        feature = StubFeature()
        self.agent = AgentFeature(StubTimeFeature(), feature, feature, feature)
        self.app = SimpleNamespace(config={"TIMEZONE": "UTC"})
        self.message = IncomingMessage(
            content="$agent tools",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        )

    async def test_lists_registered_tools(self):
        response = await self.agent.handle(self.app, self.message)

        self.assertIn("system_status", response[0].text)
        self.assertIn("create_reminder", response[0].text)

    async def test_natural_reminder_uses_registered_tool(self):
        message = self._message("$agent remind me tomorrow at this time to test")
        plan = [{"tool": "create_reminder", "arguments": {"request": "tomorrow at this time to test"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner:
            response = await self.agent.handle(self.app, message)

        planner.assert_called_once()
        self.assertIn("$agent confirm", response[0].text)

    async def test_lists_reminders_from_multilingual_plan(self):
        message = self._message("$agent 查看所有提醒")
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner:
            response = await self.agent.handle(self.app, message)

        planner.assert_called_once()
        self.assertIn("pending reminders", response[0].text)

    async def test_delete_reminder_requires_confirmation(self):
        plan = [{"tool": "delete_reminder", "arguments": {"reminder_id": 1}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await self.agent.handle(self.app, self._message("$agent remove reminder 1"))

        self.assertIn("$agent confirm", preview[0].text)
        confirmed = await self.agent.handle(self.app, self._message("$agent confirm"))
        self.assertIn("deleted reminder 1", confirmed[0].text)

    async def test_empty_plan_falls_back_to_conversation(self):
        self.app.config["CREATOR_PROMPT"] = "private context"
        message = self._message("$agent how are you?")
        message.metadata["is_creator"] = True

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="doing well") as query_chat:
            response = await self.agent.handle(self.app, message)

        self.assertEqual(response[0].text, "doing well")
        self.assertEqual(query_chat.call_args.args[4], "private context")

    async def test_conversational_image_is_forwarded_to_model(self):
        message = self._message("$agent what is this?")
        image = {
            "url": "https://cdn.discordapp.com/photo.png",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
        }
        message.metadata["image_attachments"] = [image]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="a cat") as query_chat:
            response = await self.agent.handle(self.app, message)

        self.assertEqual(response[0].text, "a cat")
        self.assertEqual(query_chat.call_args.args[6], [image])

    async def test_missing_action_context_can_request_clarification(self):
        plan = [{
            "tool": "ask_clarification",
            "arguments": {"question": "Which video should I clip?"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent clip this video"))

        self.assertIn("Which video should I clip?", response[0].text)

    async def test_progress_shows_safe_stages_and_is_removed(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent show my reminders"))

        self.assertIn("pending reminders", response[0].text)
        self.assertIn("checking available capabilities", platform.sent[0][1])
        self.assertTrue(any("selected `list reminders`" in content.lower() for _, content in platform.edited))
        self.assertTrue(any("running list reminders" in content.lower() for _, content in platform.edited))
        self.assertEqual(platform.deleted, [platform.sent[0][2]])

    async def test_executes_read_only_plan_immediately(self):
        plan = [{"tool": "system_status", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent show status"))

        self.assertIn("$system", response[0].text)

    async def test_write_plan_requires_confirmation(self):
        plan = [{"tool": "create_reminder", "arguments": {"request": "tomorrow at 8 PM to call Alex"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await self.agent.handle(self.app, self._message("$agent schedule something for tomorrow"))

        self.assertIn("$agent confirm", preview[0].text)
        confirmed = await self.agent.handle(self.app, self._message("$agent confirm"))
        self.assertIn("scheduled", confirmed[0].text)

    async def test_rejects_unregistered_planner_tool(self):
        plan = [{"tool": "run_shell", "arguments": {"command": "date"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent run a command"))

        self.assertIn("Unknown tool", response[0].text)

    async def test_queue_music_uses_typed_tool_after_confirmation(self):
        music = StubFeature([BotResponse(text="added to queue")])
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), music_feature=music)
        plan = [{"tool": "queue_music", "arguments": {"url": "https://youtu.be/example"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await agent.handle(self.app, self._message("$agent add that music to the playlist"))

        self.assertIn("$agent confirm", preview[0].text)
        result = await agent.handle(self.app, self._message("$agent confirm"))
        self.assertIn("added to queue", result[0].text)
        self.assertEqual(music.messages[0].content, '$music --youtube "https://youtu.be/example" --queue')

    async def test_planner_receives_replied_message_context(self):
        message = self._message("$agent add that music to the playlist")
        message.metadata["referenced_content"] = "https://youtu.be/example"

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]) as planner, \
                patch("features.feature_agent.unified_chat.query_chat", return_value="okay"):
            await self.agent.handle(self.app, message)

        planning_request = planner.call_args.args[0]
        self.assertIn("add that music", planning_request)
        self.assertIn("Replied-to message: https://youtu.be/example", planning_request)

    async def test_clip_tool_preserves_generated_attachment(self):
        attachment = BotAttachment(path="/tmp/clip.mp4")
        clips = StubFeature([BotResponse(text="clip ready", attachment=attachment)])
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips)
        plan = [{
            "tool": "clip_video",
            "arguments": {"url": "https://youtu.be/example", "start": "1:05", "end": "1:15"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            await agent.handle(self.app, self._message("$agent clip this video for me"))

        result = await agent.handle(self.app, self._message("$agent confirm"))
        self.assertIs(result[0].attachment, attachment)
        self.assertEqual(
            clips.messages[0].content,
            '$clip --url "https://youtu.be/example" --start "1:05" --end "1:15"',
        )

    def _message(self, content):
        return IncomingMessage(**{**self.message.__dict__, "content": content})


if __name__ == "__main__":
    unittest.main()
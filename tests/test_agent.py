from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.bot_types import BotResponse, IncomingMessage
from features.feature_agent import AgentFeature


class StubFeature:
    async def handle(self, app, message):
        return [BotResponse(text=message.content)]


class StubTimeFeature(StubFeature):
    def create_natural_reminder(self, app, message, text):
        return [BotResponse(text=f"scheduled: {text}")]


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

    async def test_natural_reminder_bypasses_llm(self):
        message = self._message("$agent remind me tomorrow at this time to test")

        with patch("features.feature_agent.unified_chat.plan_agent_actions") as planner:
            response = await self.agent.handle(self.app, message)

        planner.assert_not_called()
        self.assertIn("scheduled", response[0].text)

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

    def _message(self, content):
        return IncomingMessage(**{**self.message.__dict__, "content": content})


if __name__ == "__main__":
    unittest.main()
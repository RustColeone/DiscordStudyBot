from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from providers import unified_chat


class AgentPlannerTests(unittest.TestCase):
    def test_planner_reads_context_without_persisting_internal_exchange(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            plan = unified_chat.plan_agent_actions(
                "add that music to the playlist",
                [{"name": "queue_music"}],
                "channel",
                datetime(2026, 9, 9, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertFalse(query_chat.call_args.kwargs["persist"])

    def test_planner_prompt_explains_when_search_is_needed(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "What changed today?",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.args[0]
        self.assertIn("recently changed information", prompt)
        self.assertIn("not confident", prompt)
        self.assertIn("stable facts", prompt)


if __name__ == "__main__":
    unittest.main()
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
        self.assertFalse(query_chat.call_args.kwargs["use_history"])
        self.assertEqual(
            query_chat.call_args.kwargs["agent_effort"],
            unified_chat.collaborative_agent.AGENT_PLANNING_EFFORT,
        )

    def test_malformed_planner_output_falls_back_to_safe_conversation(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch(
                    "providers.unified_chat.query_chat",
                    side_effect=["I think no tool is needed."] * 3,
                ) as query_chat:
            plan = unified_chat.plan_agent_actions(
                "中华人民共和国宪法第35条规定公民有结社自由。",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(query_chat.call_count, 3)
        self.assertTrue(all(
            call.kwargs["use_history"] is False
            for call in query_chat.call_args_list
        ))

    def test_wordplay_uses_high_effort_internal_planning_without_tools(self):
        request = "生鱼片是死鱼片吗？等红灯其实是在等绿灯？一个半小时其实是三个半小时？"
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            plan = unified_chat.plan_agent_actions(
                request,
                [{"name": "web_search"}, {"name": "consult_luna"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(query_chat.call_args.args[0], request)
        self.assertEqual(query_chat.call_args.kwargs["agent_effort"], "high")
        planner_context = query_chat.call_args.kwargs["system_context"]
        self.assertIn("invisible internal planning turn", planner_context)
        self.assertIn("do not include a public thought bubble", planner_context)

    def test_planner_prompt_explains_when_search_is_needed(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "What changed today?",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(query_chat.call_args.args[0], "What changed today?")
        prompt = query_chat.call_args.kwargs["system_context"]
        self.assertIn("current, recent, latest, today", prompt)
        self.assertIn("prefer grounded search over a stale guess", prompt)
        self.assertIn("Do not search for casual conversation", prompt)
        self.assertIn("answer accurately from stable internal knowledge", prompt)

    def test_planner_prompt_routes_general_links_to_page_reader(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "summarize https://example.com/article",
                [{"name": "read_web_page"}, {"name": "web_search"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.kwargs["system_context"]
        self.assertIn("Choose read_web_page", prompt)
        self.assertIn("sends a bare URL directly", prompt)
        self.assertIn("Never choose both read_web_page", prompt)

    def test_planner_reads_attachment_before_visual_search(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "Find things similar to the attached image.\n\n"
                "Conversation attachment history (newest first):\n"
                "- [attachment #12] insect.jpg (image/jpeg, 1024 bytes)",
                [{"name": "read_attachment"}, {"name": "web_search"}],
                "channel",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.kwargs["system_context"]
        self.assertIn("choose read_attachment with its attachment ID", prompt)
        self.assertIn("Do not assume its contents", prompt)
        self.assertIn("comma-separated attachment argument", prompt)

    def test_planner_treats_here_as_the_active_discord_channel(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "请直接发送到这里",
                [{"name": "manage_pending_clips"}, {"name": "ask_clarification"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.kwargs["system_context"]
        self.assertIn("current Discord channel is always the implicit destination", prompt)
        self.assertIn("直接发送 do not require a recipient", prompt)

    def test_planner_invites_each_explicitly_addressed_teammate(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("providers.unified_chat.query_chat", return_value="[]") as query_chat:
            unified_chat.plan_agent_actions(
                "Luna, Gemini, and Claude, help DeepSeek compare these options",
                [{"name": "consult_luna"}, {"name": "consult_gemini"}, {"name": "consult_claude"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.kwargs["system_context"]
        self.assertIn("multiple named teammates as explicit invitations", prompt)
        self.assertIn("select each requested collaborator", prompt)
        self.assertIn("Choose consult_claude", prompt)

    def test_progress_reviewer_returns_typed_decision_without_persisting(self):
        response = (
            '{"status":"continue","summary":"The preview is too large.",'
            '"actions":[{"tool":"adjust_pending_clip","arguments":{"action":"fit_discord"}}],'
            '"question":""}'
        )
        with patch("providers.unified_chat.query_chat", return_value=response) as query_chat:
            review = unified_chat.review_agent_progress(
                "Make this video fit Discord",
                [{"name": "adjust_pending_clip"}],
                [{"tool": "clip_video", "arguments": {"url": "https://youtu.be/example"}}],
                ["Estimated size: 96.56MB"],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(review["status"], "continue")
        self.assertEqual(review["actions"][0]["tool"], "adjust_pending_clip")
        self.assertFalse(query_chat.call_args.kwargs["persist"])
        self.assertIn("observable tool outcomes", query_chat.call_args.args[0])

    def test_progress_reviewer_rejects_inconsistent_decision(self):
        response = '{"status":"complete","summary":"Done.","actions":[],"question":"Still proceed?"}'
        with patch("providers.unified_chat.query_chat", return_value=response):
            with self.assertRaisesRegex(ValueError, "only clarify may include a question"):
                unified_chat.review_agent_progress(
                    "Do the task",
                    [],
                    [],
                    ["Result"],
                    "channel",
                    datetime(2026, 9, 10, tzinfo=timezone.utc),
                )

    def test_progress_reviewer_knows_generated_attachment_destination(self):
        response = '{"status":"complete","summary":"Queued here.","actions":[],"question":""}'
        with patch("providers.unified_chat.query_chat", return_value=response) as query_chat:
            unified_chat.review_agent_progress(
                "请直接发送",
                [],
                [],
                ["Rendered clip attachment"],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_chat.call_args.args[0]
        self.assertIn("delivered to the current Discord channel", prompt)
        self.assertIn("must not trigger a recipient clarification", prompt)


if __name__ == "__main__":
    unittest.main()
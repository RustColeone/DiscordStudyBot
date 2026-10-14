from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch

from providers import unified_chat


class AgentPlannerTests(unittest.TestCase):
    def setUp(self):
        route = patch.object(unified_chat.collaborative_agent, "query_room_route", return_value=
                             '{"route":"task","participants":[],"effort":"medium"}')
        route.start()
        self.addCleanup(route.stop)

    def test_photo_conversation_skips_full_planning_and_invites_plural_addressees(self):
        with patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(unified_chat.collaborative_agent, "query_room_route", return_value=json.dumps({
                    "route": "conversation", "participants": [], "effort": "low",
                })) as route, \
                patch.object(unified_chat.collaborative_agent, "query_luna_control") as full_plan:
            plan = unified_chat.plan_agent_actions(
                "烧卖你们觉得好吃吗？这个是我做的", [{"name": "read_attachment"}], "channel", None,
                current_attachment_ids=[42],
            )
        full_plan.assert_not_called()
        route.assert_called_once()
        self.assertEqual(plan, [{"tool": "read_attachment", "arguments": {"attachment": "42"}}])
        self.assertTrue(plan.conversation_only)
        self.assertEqual(plan.reasoning_effort, "low")
        self.assertEqual(plan.participants, ["Luna", "Gemini", "Claude"])

    def test_action_request_still_gets_full_validated_planning(self):
        with patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(unified_chat.collaborative_agent, "query_room_route", return_value=json.dumps({
                    "route": "task", "participants": [], "effort": "medium",
                })), patch.object(unified_chat.collaborative_agent, "query_luna_control", return_value=json.dumps({
                    "response_mode": "conversation", "response_depth": "concise", "participants": [],
                    "actions": [{"tool": "system_status", "arguments": {}}],
                })) as full_plan:
            plan = unified_chat.plan_agent_actions("Show server status", [{"name": "system_status"}], "channel", None)
        full_plan.assert_called_once()
        self.assertEqual(plan[0]["tool"], "system_status")
        self.assertFalse(plan.conversation_only)

    def test_invalid_lightweight_route_falls_back_to_validated_planner(self):
        with patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(unified_chat.collaborative_agent, "query_room_route", return_value=json.dumps({
                    "route": "conversation", "participants": ["Unknown"], "effort": "low",
                })), patch.object(unified_chat.collaborative_agent, "query_luna_control", return_value="[]") as full_plan:
            plan = unified_chat.plan_agent_actions("hello", [], "channel", None)
        self.assertEqual(plan, [])
        full_plan.assert_called_once()
        self.assertEqual(full_plan.call_args.args[2], "medium")
        self.assertEqual(full_plan.call_args.kwargs["request_timeout"], 35.0)

    def test_router_timeout_does_not_trigger_repeated_max_effort_planning(self):
        with patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(unified_chat.collaborative_agent, "query_room_route", side_effect=TimeoutError), \
                patch.object(unified_chat.collaborative_agent, "query_luna_control", return_value="invalid") as planner:
            plan = unified_chat.plan_agent_actions("hello", [], "channel", None)
        self.assertEqual(plan, [])
        planner.assert_called_once()
        self.assertEqual(planner.call_args.args[2], "medium")
        self.assertEqual(planner.call_args.kwargs["request_timeout"], 35.0)

    def test_planner_returns_semantic_response_depth(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value=(
                        '{"response_mode":"deliverable","participants":[],"response_depth":"detailed","approach":'
                        '"Trace the power-stage dynamics, then compare feedback paths.","actions":[]}'
                    ),
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                "Develop a detailed proof and discuss every important edge case.",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 15, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(plan.response_depth, "detailed")
        self.assertEqual(
            plan.approach,
            "Trace the power-stage dynamics, then compare feedback paths.",
        )
        planner_context = query_luna.call_args.args[1]
        self.assertIn("response_depth", planner_context)
        self.assertIn("user-visible outline", planner_context)
        self.assertIn("Do not use phrase matching", planner_context)

    def test_planner_reads_context_without_persisting_internal_exchange(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                "add that music to the playlist",
                [{"name": "queue_music"}],
                "channel",
                datetime(2026, 9, 9, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(
            query_luna.call_args.args[2],
            unified_chat.collaborative_agent.AGENT_PLANNING_EFFORT,
        )

    def test_complex_conversation_stays_brief_and_can_include_a_playful_participant(self):
        with patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(unified_chat, "conversation_context", return_value="[User]: Starting is hard."), \
                patch.object(unified_chat.collaborative_agent, "query_luna_control", return_value=json.dumps({
                    "response_mode": "conversation", "response_depth": "detailed", "approach": "",
                    "participants": ["Gemini"], "actions": [],
                })) as planner:
            plan = unified_chat.plan_agent_actions(
                "Why do I procrastinate even though studying matters to me?", [], "channel", None,
            )

        self.assertEqual(plan.response_mode, "conversation")
        self.assertEqual(plan.response_depth, "concise")
        self.assertEqual(plan.participants, ["Gemini"])
        self.assertIn("[User]: Starting is hard.", planner.call_args.args[1])

    def test_planner_rejects_unknown_or_duplicate_participants(self):
        for participants in (["Unknown"], ["Gemini", "Gemini"], "Gemini"):
            with self.subTest(participants=participants), \
                    patch.object(unified_chat.db, "get_channel_settings", return_value={"mode": "agent"}), \
                    patch.object(unified_chat.collaborative_agent, "query_luna_control", return_value=json.dumps({
                        "response_mode": "conversation", "response_depth": "concise",
                        "participants": participants, "actions": [],
                    })):
                self.assertEqual(unified_chat.plan_agent_actions("hello", [], "channel", None), [])

    def test_malformed_planner_output_falls_back_to_safe_conversation(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch(
                    "providers.unified_chat.collaborative_agent.query_luna_control",
                    side_effect=["I think no tool is needed."] * 3,
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                "中华人民共和国宪法第35条规定公民有结社自由。",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(query_luna.call_count, 3)

    def test_wordplay_uses_max_effort_internal_planning_without_tools(self):
        request = "生鱼片是死鱼片吗？等红灯其实是在等绿灯？一个半小时其实是三个半小时？"
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                request,
                [{"name": "web_search"}, {"name": "consult_luna"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(plan, [])
        self.assertEqual(query_luna.call_args.args[0], request)
        self.assertEqual(query_luna.call_args.args[2], "max")
        planner_context = query_luna.call_args.args[1]
        self.assertIn("invisible internal planning turn", planner_context)
        self.assertIn("do not include a public thought bubble", planner_context)

    def test_planner_prompt_explains_when_search_is_needed(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "What changed today?",
                [{"name": "web_search"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        self.assertEqual(query_luna.call_args.args[0], "What changed today?")
        prompt = query_luna.call_args.args[1]
        self.assertIn("current, recent, latest, today", prompt)
        self.assertIn("prefer grounded search over a stale guess", prompt)
        self.assertIn("Do not search for casual conversation", prompt)
        self.assertIn("answer accurately from stable internal knowledge", prompt)
        self.assertIn("named provisions, sections, chapters, or passages", prompt)
        self.assertIn("search excerpts alone are not the requested document content", prompt)

    def test_planner_receives_authoritative_time_and_structures_relative_duration(self):
        runtime_context = (
            "Configured local datetime: 2026-09-14 13:41:00 Monday PDT-0700\n"
            "Configured IANA timezone: America/Los_Angeles"
        )
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value=(
                        '[{"tool":"create_reminder","arguments":{"message":"微波炉",'
                        '"schedule_type":"relative","delay_value":2,"delay_unit":"hours"}}]'
                    ),
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                "两个小时之后提醒微波炉",
                [{"name": "create_reminder"}],
                "channel",
                datetime(2026, 9, 14, 20, 41, tzinfo=timezone.utc),
                runtime_context,
            )

        self.assertEqual(plan[0]["arguments"]["message"], "微波炉")
        self.assertEqual(plan[0]["arguments"]["delay_value"], 2)
        self.assertEqual(plan[0]["arguments"]["delay_unit"], "hours")
        planner_context = query_luna.call_args.args[1]
        self.assertIn(runtime_context, planner_context)
        self.assertIn("regardless of language", planner_context)
        self.assertIn("structured schedule fields", planner_context)

    def test_planner_prompt_routes_general_links_to_page_reader(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"effort": "high"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "summarize https://example.com/article",
                [{"name": "read_web_page"}, {"name": "web_search"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_luna.call_args.args[1]
        self.assertIn("Choose read_web_page", prompt)
        self.assertIn("sends a bare URL directly", prompt)
        self.assertIn("Never choose both read_web_page", prompt)

    def test_planner_reads_attachment_before_visual_search(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "Find things similar to the attached image.\n\n"
                "Conversation attachment history (newest first):\n"
                "- [attachment #12] insect.jpg (image/jpeg, 1024 bytes)",
                [{"name": "read_attachment"}, {"name": "web_search"}],
                "channel",
                datetime(2026, 9, 11, tzinfo=timezone.utc),
            )

        prompt = query_luna.call_args.args[1]
        self.assertIn("choose read_attachment with its attachment ID", prompt)
        self.assertIn("Do not assume its contents", prompt)
        self.assertIn("comma-separated attachment argument", prompt)

    def test_planner_treats_here_as_the_active_discord_channel(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "请直接发送到这里",
                [{"name": "manage_pending_clips"}, {"name": "ask_clarification"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_luna.call_args.args[1]
        self.assertIn("current Discord channel is always the implicit destination", prompt)
        self.assertIn("直接发送 do not require a recipient", prompt)

    def test_planner_invites_each_explicitly_addressed_teammate(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "Luna, Gemini, and Claude, help DeepSeek compare these options",
                [{"name": "consult_luna"}, {"name": "consult_gemini"}, {"name": "consult_claude"}],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_luna.call_args.args[1]
        self.assertIn("multiple named teammates as explicit invitations", prompt)
        self.assertIn("select each requested collaborator", prompt)
        self.assertIn("Choose consult_claude", prompt)

    def test_planner_assigns_image_generation_to_gemini(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value='[{"tool":"generate_image","arguments":{"prompt":"anime library"}}]',
                ) as query_luna:
            plan = unified_chat.plan_agent_actions(
                "draw this for me in anime style",
                [{"name": "generate_image"}],
                "channel",
                datetime(2026, 9, 14, tzinfo=timezone.utc),
            )

        self.assertEqual(plan[0]["tool"], "generate_image")
        planner_prompt = query_luna.call_args.args[1]
        self.assertIn("Image generation belongs to Gemini", planner_prompt)
        self.assertIn("expects an image file", planner_prompt)

    def test_planner_distinguishes_existing_image_search_from_generation(self):
        with patch("providers.unified_chat.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(
                    unified_chat.collaborative_agent,
                    "query_luna_control",
                    return_value="[]",
                ) as query_luna:
            unified_chat.plan_agent_actions(
                "Show a useful real diagram if it materially helps",
                [{"name": "image_search"}, {"name": "generate_image"}],
                "channel",
                datetime(2026, 9, 14, tzinfo=timezone.utc),
            )

        planner_prompt = query_luna.call_args.args[1]
        self.assertIn("Choose image_search only when one specific real image", planner_prompt)
        self.assertIn("Prefer image_search for an existing real-world visual", planner_prompt)

    def test_progress_reviewer_returns_typed_decision_without_persisting(self):
        response = (
            '{"status":"continue","summary":"The preview is too large.",'
            '"actions":[{"tool":"adjust_pending_clip","arguments":{"action":"fit_discord"}}],'
            '"question":""}'
        )
        with patch.object(
            unified_chat.collaborative_agent,
            "query_luna_control",
            return_value=response,
        ) as query_luna:
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
        self.assertEqual(query_luna.call_args.args[2], "max")
        self.assertIn("observable tool outcomes", query_luna.call_args.args[0])

    def test_progress_reviewer_rejects_inconsistent_decision(self):
        response = '{"status":"complete","summary":"Done.","actions":[],"question":"Still proceed?"}'
        with patch.object(
            unified_chat.collaborative_agent,
            "query_luna_control",
            return_value=response,
        ):
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
        with patch.object(
            unified_chat.collaborative_agent,
            "query_luna_control",
            return_value=response,
        ) as query_luna:
            unified_chat.review_agent_progress(
                "请直接发送",
                [],
                [],
                ["Rendered clip attachment"],
                "channel",
                datetime(2026, 9, 10, tzinfo=timezone.utc),
            )

        prompt = query_luna.call_args.args[0]
        self.assertIn("delivered to the current Discord channel", prompt)
        self.assertIn("must not trigger a recipient clarification", prompt)

    def test_progress_reviewer_must_open_discovered_document_source(self):
        response = (
            '{"status":"continue","summary":"The search located the ordinance but not the requested text.",'
            '"actions":[{"tool":"read_web_page","arguments":{"url":"https://example.com/cap219.pdf"}}],'
            '"question":""}'
        )
        outcomes = [
            'Search excerpt with titles only.\nCandidate source URLs returned by this tool:\n'
            '[{"title":"Cap. 219 PDF","url":"https://example.com/cap219.pdf"}]'
        ]
        with patch.object(
            unified_chat.collaborative_agent,
            "query_luna_control",
            return_value=response,
        ) as query_luna:
            review = unified_chat.review_agent_progress(
                "Summarize sections 4 and 5 of Cap. 219",
                [{"name": "read_web_page"}],
                [{"tool": "web_search", "arguments": {"query": "Cap. 219 sections 4 5"}}],
                outcomes,
                "channel",
                datetime(2026, 9, 14, tzinfo=timezone.utc),
            )

        self.assertEqual(review["actions"][0]["tool"], "read_web_page")
        prompt = query_luna.call_args.args[0]
        self.assertIn("Search excerpts and lists of candidate sources are discovery results", prompt)
        self.assertIn("https://example.com/cap219.pdf", prompt)


if __name__ == "__main__":
    unittest.main()

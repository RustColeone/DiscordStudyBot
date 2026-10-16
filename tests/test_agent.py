import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.bot_types import BotAttachment, BotResponse, IncomingMessage
from features.feature_agent import AgentFeature
from features.feature_clip import ClipFeature
from features.feature_time import TimeFeature
from providers import unified_chat
from providers.unified_chat import AgentPlan
from services.agent_task import AgentTask
from services import database as db
from services.character_diary import diary_scope
from features.feature_clip import ClipFeature
from providers import clip_query


class StubFeature:
    def __init__(self, response=None):
        self.response = response
        self.messages = []

    async def handle(self, app, message):
        self.messages.append(message)
        return self.response or [BotResponse(text=message.content)]


class StubTimeFeature(StubFeature):
    def __init__(self, response=None):
        super().__init__(response)
        self.reminder_requests = []

    def create_structured_reminder(self, app, message, **schedule):
        self.reminder_requests.append((message, schedule))
        return [BotResponse(text=f"scheduled: {schedule['reminder_text']}")]

    def list_reminders(self, message, include_all=False):
        return "pending reminders"

    def delete_reminder(self, message, reminder_id):
        return f"deleted reminder {reminder_id}"


class StubSearchFeature(StubFeature):
    def __init__(self):
        super().__init__()
        self.web_requests = []
        self.page_requests = []

    async def answer_with_web(self, message, question, search_query):
        self.web_requests.append((message, question, search_query))
        return [BotResponse(text="Synthesized current answer [1].\n\n**Sources**\n[1] Source")]

    async def answer_from_pages(self, message, question, urls):
        self.page_requests.append((message, question, urls))
        return [BotResponse(text="Combined page summary [1][2].")]


class StubDrawFeature:
    def __init__(self):
        self.requests = []

    async def generate(self, message, description, **options):
        self.requests.append((message, description, options))
        return [BotResponse(
            text="**Gemini**\nI finished the image.",
            attachment=BotAttachment(
                path="/tmp/generated.png",
                filename="generated.png",
                delete_after_send=True,
            ),
            metadata={"generated_by": "Gemini", "generated_image": True},
        )]


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


class StubLiveRoomPlatform:
    supports_progress = False
    supports_live_responses = True

    def __init__(self):
        self.sent = []
        self.deleted = []
        self.sent_responses = []

    async def send_channel_message(self, channel_id, content):
        self.sent.append((channel_id, content))
        return content

    async def send_channel_response(self, channel_id, response):
        self.sent_responses.append(response)
        self.sent.append((channel_id, response.text))
        return response.text or response.attachment

    async def delete_message(self, message):
        self.deleted.append(message)


class LegacyAgentFeatureTests(unittest.IsolatedAsyncioTestCase):
    """Coverage of retained planner/roleplay helpers; active room tests live in test_shared_room."""
    def setUp(self):
        self.settings_patcher = patch(
            "features.feature_agent.db.get_channel_settings",
            return_value={"mode": "roleplay"},
        )
        self.settings_patcher.start()
        self.addCleanup(self.settings_patcher.stop)
        self.claude_patcher = patch(
            "features.feature_agent.collaborative_agent.consult_claude",
            return_value="SKIP",
        )
        self.claude_patcher.start()
        self.addCleanup(self.claude_patcher.stop)
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
        response = await self.agent._handle_legacy(self.app, self.message)

        self.assertIn("system_status", response[0].text)
        self.assertIn("create_reminder", response[0].text)
        self.assertIn("read_web_page", response[0].text)

    async def test_natural_reminder_uses_registered_tool(self):
        message = self._message("$agent remind me tomorrow at this time to test")
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "test",
                "schedule_type": "absolute",
                "remind_at": "2026-09-10T00:00:00+00:00",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner:
            response = await self.agent._handle_legacy(self.app, message)

        planner.assert_called_once()
        runtime_context = planner.call_args.args[4]
        self.assertIn("2026-09-09 00:00:00 Wednesday UTC+0000", runtime_context)
        self.assertIn("Configured IANA timezone: UTC", runtime_context)
        self.assertIn("$agent confirm", response[0].text)

    async def test_image_request_is_executed_by_gemini_and_saved_to_room_history(self):
        draw = StubDrawFeature()
        agent = AgentFeature(
            StubTimeFeature(), StubFeature(), StubSearchFeature(), StubFeature(),
            draw_feature=draw,
        )
        plan = [{
            "tool": "generate_image",
            "arguments": {"prompt": "An anime-style moonlit library"},
        }]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save_exchange:
            responses = await agent._handle_legacy(
                self.app,
                self._message("$agent draw a moonlit library in anime style"),
            )

        self.assertEqual(draw.requests[0][1], "An anime-style moonlit library")
        self.assertEqual(draw.requests[0][2], {"provider": "gemini"})
        self.assertEqual(responses[0].text, "**Gemini**\nI finished the image.")
        self.assertEqual(responses[0].attachment.filename, "generated.png")
        save_exchange.assert_called_once()
        self.assertIn("Gemini", save_exchange.call_args.args[1])

    async def test_generate_image_tool_can_select_openai_when_user_asks_for_gpt(self):
        draw = StubDrawFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubSearchFeature(), StubFeature(), draw_feature=draw)
        tool = agent.registry.get("generate_image")
        self.assertEqual(tool.parameters["provider"]["enum"], ["gemini", "openai"])
        agent.registry.validate_arguments(tool, {"prompt": "dragon girl", "provider": "openai"})
        with self.assertRaises(ValueError):
            agent.registry.validate_arguments(tool, {"prompt": "dragon girl", "provider": "midjourney"})
        await agent._generate_image(self.app, self._message("draw"), {"prompt": "dragon girl", "provider": "openai"})
        await agent._generate_image(self.app, self._message("draw"), {"prompt": "dragon girl"})
        self.assertEqual([request[2] for request in draw.requests[-2:]], [{"provider": "openai"}, {"provider": "gemini"}])

    async def test_lists_reminders_from_multilingual_plan(self):
        message = self._message("$agent 查看所有提醒")
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner:
            response = await self.agent._handle_legacy(self.app, message)

        planner.assert_called_once()
        self.assertIn("pending reminders", response[0].text)

    async def test_delete_reminder_requires_confirmation(self):
        plan = [{"tool": "delete_reminder", "arguments": {"reminder_id": 1}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await self.agent._handle_legacy(self.app, self._message("$agent remove reminder 1"))

        self.assertIn("$agent confirm", preview[0].text)
        confirmed = await self.agent._handle_legacy(self.app, self._message("$agent confirm"))
        self.assertIn("deleted reminder 1", confirmed[0].text)

    async def test_empty_plan_falls_back_to_conversation(self):
        self.app.config["CREATOR_PROMPT"] = "private context"
        message = self._message("$agent how are you?")
        message.metadata["is_creator"] = True

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="doing well") as query_chat:
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(response[0].text, "doing well")
        system_context = query_chat.call_args.args[4]
        self.assertIn("Authoritative request-time context", system_context)
        self.assertIn("Configured IANA timezone: UTC", system_context)
        self.assertIn("private context", system_context)

    async def test_malformed_internal_planning_still_reaches_conversation(self):
        message = self._message(
            "$agent 中华人民共和国宪法第35条规定公民有结社自由。"
        )

        with patch(
            "features.feature_agent.db.get_channel_settings",
            return_value={"mode": "roleplay", "effort": "high"},
        ), patch(
            "providers.unified_chat.collaborative_agent.query_luna_control",
            side_effect=["No tool needed."] * 3,
        ) as query_luna, patch(
            "features.feature_agent.unified_chat.query_chat",
            return_value="这是一个需要区分法律文本与实际执行的问题。",
        ) as query_chat:
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(response[0].text, "这是一个需要区分法律文本与实际执行的问题。")
        self.assertEqual(query_luna.call_count, 3)
        query_chat.assert_called_once()

    async def test_conversational_image_is_fetched_before_vision(self):
        message = self._message("$agent what is this?")
        image = {
            "message_id": "100",
            "attachment_id": "200",
            "author_id": "user",
            "url": "https://cdn.discordapp.com/photo.png",
            "filename": "photo.png",
            "content_type": "image/png",
            "size": 1024,
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        record = {**image, "id": 1, "channel_id": "channel"}
        fetched_image = {**image, "path": "/tmp/cached-photo.png"}
        message.metadata["attachments"] = [image]
        plan = [{"tool": "read_attachment", "arguments": {"attachment": "1"}}]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.db.list_conversation_attachments", return_value=[record]), \
                patch("features.feature_agent.db.find_conversation_attachment", return_value=record), \
                patch("features.feature_agent.attachment_store.read_attachment", return_value={
                    "id": 1,
                    "filename": "photo.png",
                    "content_type": "image/png",
                    "size": 1024,
                    "cached_path": fetched_image["path"],
                    "text": "",
                    "image": fetched_image,
                }) as read_attachment, \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.review_agent_progress", return_value={
                    "status": "complete", "summary": "The image is available.",
                    "actions": [], "question": "",
                }), patch("features.feature_agent.unified_chat.query_chat", return_value="a cat") as query_chat, \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_deepseek", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(response[0].text, "**DeepSeek**\na cat")
        read_attachment.assert_called_once_with(record)
        self.assertEqual(query_chat.call_args.args[6], [fetched_image])

    async def test_missing_action_context_can_request_clarification(self):
        plan = [{
            "tool": "ask_clarification",
            "arguments": {"question": "Which video should I clip?"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent._handle_legacy(self.app, self._message("$agent clip this video"))

        self.assertIn("Which video should I clip?", response[0].text)

    async def test_image_answer_resumes_clarified_search_goal(self):
        clarification = [{
            "tool": "ask_clarification",
            "arguments": {"question": "你想查找哪一项类似的东西？"},
        }]
        image = {
            "message_id": "101",
            "attachment_id": "201",
            "author_id": "user",
            "url": "https://cdn.discordapp.com/insect.jpg",
            "filename": "insect.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        record = {**image, "id": 7, "channel_id": "channel"}
        fetched_image = {**image, "path": "/tmp/cached-insect.jpg"}
        read_plan = [{"tool": "read_attachment", "arguments": {"attachment": "7"}}]
        grounding = SimpleNamespace(
            text="Candidate insects include small ant-like beetles.",
            sources=[{
                "title": "Ant-like beetles",
                "url": "https://example.com/insects",
                "provider": "Google Search",
            }],
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.db.list_conversation_attachments", side_effect=[[], [record]]), \
                patch("features.feature_agent.db.find_conversation_attachment", return_value=record), \
                patch("features.feature_agent.attachment_store.read_attachment", return_value={
                    "id": 7,
                    "filename": "insect.jpg",
                    "content_type": "image/jpeg",
                    "size": 1024,
                    "cached_path": fetched_image["path"],
                    "text": "",
                    "image": fetched_image,
                }), \
                patch(
                    "features.feature_agent.unified_chat.plan_agent_actions",
                    side_effect=[clarification, read_plan],
                ) as planner, patch(
                    "features.feature_agent.unified_chat.query_chat",
                    side_effect=[
                        "我先根据图片特征查找候选。 <<SEARCH:small dark ant-like insect>>",
                        "找到几类外形相近的候选，但照片不足以定种。",
                    ],
                ) as query_chat, patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    return_value={
                        "status": "complete", "summary": "The image is ready.",
                        "actions": [], "question": "",
                    },
                ), patch(
                    "features.feature_agent.collaborative_agent.ground_with_google",
                    return_value=grounding,
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["SKIP", "候选与来源一致，但不能仅凭模糊照片定种。"],
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            first = await self.agent._handle_legacy(
                self.app, self._message("$agent 你们查一下有没有类似的")
            )
            follow_up = self._message("$agent 刚才图片里的")
            follow_up.metadata["attachments"] = [image]
            second = await self.agent._handle_legacy(self.app, follow_up)

        self.assertEqual(first[0].text, "**DeepSeek**\n你想查找哪一项类似的东西？")
        self.assertNotIn("ask_clarification", first[0].text)
        resumed_planning = planner.call_args_list[1].args[0]
        self.assertIn("Original goal: 你们查一下有没有类似的", resumed_planning)
        self.assertIn("User's answer to the pending question: 刚才图片里的", resumed_planning)
        self.assertIn("[attachment #7] insect.jpg", resumed_planning)
        self.assertEqual(query_chat.call_args_list[0].args[0], "你们查一下有没有类似的")
        self.assertEqual(query_chat.call_args_list[0].args[6], [fetched_image])
        visible = "\n".join(response.text or "" for response in second)
        self.assertIn("找到几类外形相近的候选", visible)
        self.assertIn("https://example.com/insects", visible)
        self.assertNotIn("ask_clarification", visible)

    async def test_attachment_history_is_reused_without_reference_expiry(self):
        record = {
            "id": 7,
            "channel_id": "channel",
            "message_id": "101",
            "attachment_id": "201",
            "author_id": "user",
            "url": "https://cdn.discordapp.com/insect.jpg",
            "filename": "insect.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
            "created_at": "2026-09-01T00:00:00+00:00",
        }
        plan = [{"tool": "read_attachment", "arguments": {"attachment": "7"}}]
        with patch("features.feature_agent.db.list_conversation_attachments", return_value=[record]), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner, \
                patch("features.feature_agent.db.find_conversation_attachment", return_value=record), \
                patch("features.feature_agent.attachment_store.read_attachment", return_value={
                    "id": 7, "filename": "insect.jpg", "content_type": "image/jpeg",
                    "size": 1024, "cached_path": "/tmp/insect.jpg", "text": "",
                    "image": {**record, "path": "/tmp/insect.jpg"},
                }):
            response = await self.agent._handle_legacy(
                self.app, self._message("$agent 查一下之前图片里有没有类似的")
            )

        self.assertIn("[attachment #7] insect.jpg", planner.call_args.args[0])
        self.assertIn("ready for vision inspection", response[0].text)

    async def test_multiple_attachment_images_are_fetched_in_one_action(self):
        records = [
            {
                "id": attachment_id,
                "channel_id": "channel",
                "message_id": "100",
                "attachment_id": str(200 + attachment_id),
                "author_id": "user",
                "url": f"https://cdn.discordapp.com/{filename}",
                "filename": filename,
                "content_type": "image/png",
                "size": 1024,
                "created_at": "2026-09-11T00:00:00+00:00",
            }
            for attachment_id, filename in ((1, "front.png"), (2, "back.png"))
        ]
        fetched = [
            {**record, "path": f"/tmp/{record['filename']}"}
            for record in records
        ]
        plan = [{"tool": "read_attachment", "arguments": {"attachment": "1,2"}}]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.db.list_conversation_attachments", return_value=records), \
                patch(
                    "features.feature_agent.db.find_conversation_attachment",
                    side_effect=records,
                ), patch(
                    "features.feature_agent.attachment_store.read_attachment",
                    side_effect=[
                        {
                            "id": record["id"], "filename": record["filename"],
                            "content_type": "image/png", "size": 1024,
                            "cached_path": image["path"], "text": "", "image": image,
                        }
                        for record, image in zip(records, fetched)
                    ],
                ), patch(
                    "features.feature_agent.unified_chat.plan_agent_actions", return_value=plan
                ), patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    return_value={
                        "status": "complete", "summary": "Both images are ready.",
                        "actions": [], "question": "",
                    },
                ), patch(
                    "features.feature_agent.unified_chat.query_chat", return_value="Two views."
                ) as query_chat, patch(
                    "features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek", return_value="SKIP"
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(
                self.app, self._message("$agent compare attachments 1 and 2")
            )

        self.assertEqual(query_chat.call_args.args[6], fetched)
        self.assertEqual(responses[0].text, "**DeepSeek**\nTwo views.")

    async def test_progress_shows_safe_stages_and_is_removed(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent._handle_legacy(self.app, self._message("$agent show my reminders"))

        self.assertIn("pending reminders", response[0].text)
        self.assertIn("deciding whether to invite Luna, Gemini, or Claude", platform.sent[0][1])
        self.assertTrue(any("plan: try `list reminders`" in content.lower() for _, content in platform.edited))
        self.assertTrue(any("deepseek is running list reminders" in content.lower() for _, content in platform.edited))
        self.assertEqual(platform.deleted, [platform.sent[0][2]])

    async def test_slow_generation_shows_public_outline_and_elapsed_heartbeat(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        progress = object()
        task = AgentTask(
            goal="Explain converter stability",
            response_depth="detailed",
            response_mode="deliverable",
            approach="Compare the power-stage poles, zeros, and feedback sampling point.",
        )

        async def delayed_answer():
            await asyncio.sleep(0.025)
            return "done"

        with patch.object(AgentFeature, "GENERATION_PROGRESS_INTERVAL_SECONDS", 0.005):
            result = await self.agent._await_generation(
                self.app,
                progress,
                None,
                delayed_answer(),
                self.agent._generation_progress_status("DeepSeek", task),
            )

        self.assertEqual(result, "done")
        messages = [content for _, content in platform.edited]
        self.assertTrue(messages[0].startswith("Thinking:\n```text\nDeepSeek:"))
        self.assertIn("power-stage poles, zeros", messages[0])
        heartbeat = next(content for content in messages if "Still working" in content)
        self.assertIn("elapsed", heartbeat)
        self.assertTrue(heartbeat.endswith("\n```"))

    async def test_agent_mode_removes_transient_progress_when_review_is_blocked(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]
        review = {
            "status": "clarify",
            "summary": "I found two reminders, but the requested one is ambiguous.",
            "actions": [],
            "question": "Which reminder should I use?",
        }

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.review_agent_progress", return_value=review):
            response = await self.agent._handle_legacy(self.app, self._message("$agent handle my reminder"))

        self.assertIn("Which reminder should I use?", response[-1].text)
        self.assertEqual(platform.deleted, [platform.sent[0][2]])
        self.assertTrue(all("**Process notes (live)**" not in content for _, content in platform.edited))

    async def test_agent_mode_continues_after_reviewing_incomplete_result(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]
        reviews = [
            {
                "status": "continue",
                "summary": "I found the reminder; I still need the current time to answer.",
                "actions": [{"tool": "current_time", "arguments": {}}],
                "question": "",
            },
            {
                "status": "clarify",
                "summary": "I checked both pieces, but the timezone is still missing.",
                "actions": [],
                "question": "Which timezone should I use?",
            },
        ]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    side_effect=reviews,
                ) as reviewer:
            response = await self.agent._handle_legacy(self.app, self._message("$agent when is my reminder?"))

        self.assertIn("Which timezone should I use?", response[-1].text)
        self.assertEqual(reviewer.call_count, 2)
        self.assertIn("pending reminders", reviewer.call_args_list[1].args[3][0])
        self.assertTrue(any("$time" in outcome for outcome in reviewer.call_args_list[1].args[3]))
        self.assertTrue(any("Continuing with `current time`" in content for _, content in platform.edited))

    async def test_document_search_continues_into_discovered_source(self):
        source_url = "https://casebyte.ai/legislation/219/part-P2/s4"
        search_response = BotResponse(
            text="Search result excerpt identifies section titles but does not contain the provisions.",
            metadata={
                "tool_evidence": True,
                "sources": [{
                    "title": "Cap. 219 section 4",
                    "url": source_url,
                    "provider": "Web",
                }],
            },
        )
        page_response = BotResponse(
            text="Linked-page evidence:\nSection 4. A legal estate in land may be disposed of only by deed.",
            metadata={
                "tool_evidence": True,
                "sources": [{
                    "title": "Cap. 219 section 4",
                    "url": source_url,
                    "provider": "Linked page",
                }],
            },
        )
        reviews = [
            {
                "status": "continue",
                "summary": "The search found the source; its text still needs to be read.",
                "actions": [{"tool": "read_web_page", "arguments": {"url": source_url}}],
                "question": "",
            },
            {
                "status": "complete",
                "summary": "The requested provision is now available for summarization.",
                "actions": [],
                "question": "",
            },
        ]
        plan = [{"tool": "web_search", "arguments": {"query": "Cap. 219 sections 4 and 5"}}]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch.object(self.agent, "_web_search", return_value=[search_response]), \
                patch.object(self.agent, "_read_web_page", return_value=[page_response]) as read_page, \
                patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    side_effect=reviews,
                ) as reviewer:
            responses, stopped = await self.agent._execute_goal_plan(
                self.app,
                self._message("$agent Summarize sections 4 and 5 of Cap. 219"),
                "Summarize sections 4 and 5 of Cap. 219",
                plan,
                "channel:user",
            )

        self.assertFalse(stopped)
        self.assertEqual(len(responses), 2)
        self.assertIn(source_url, reviewer.call_args_list[0].args[3][0])
        read_page.assert_awaited_once()
        self.assertEqual(read_page.await_args.args[2]["url"], [source_url])
        self.assertIn("Section 4", reviewer.call_args_list[1].args[3][1])

    async def test_reviewed_write_action_requires_confirmation_and_resumes(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]
        reviews = [
            {
                "status": "continue",
                "summary": "No matching reminder exists, so creating it is the remaining step.",
                "actions": [{
                    "tool": "create_reminder",
                    "arguments": {
                        "message": "call Alex",
                        "schedule_type": "absolute",
                        "remind_at": "2026-09-10T20:00:00+00:00",
                    },
                }],
                "question": "",
            },
            {
                "status": "clarify",
                "summary": "The reminder was created; I need no more tool actions.",
                "actions": [],
                "question": "Would you like anything else changed?",
            },
        ]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    side_effect=reviews,
                ):
            preview = await self.agent._handle_legacy(self.app, self._message("$agent ensure I call Alex tomorrow"))
            confirmed = await self.agent._handle_legacy(self.app, self._message("$agent confirm"))

        self.assertIn("need your approval", preview[-1].text)
        self.assertIn("create_reminder", preview[-1].text)
        self.assertIn("The reminder was created", confirmed[-1].text)
        self.assertEqual(len(self.agent.time_feature.messages), 0)

    async def test_rendered_attachment_is_terminal_delivery_to_current_channel(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        attachment = BotAttachment(path="/tmp/clip.mp4")
        clips = StubFeature([BotResponse(text="clip ready", attachment=attachment)])
        agent = AgentFeature(
            StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips
        )
        plan = [{"tool": "manage_pending_clips", "arguments": {"action": "confirm"}}]
        key = "channel:user"
        agent.pending_plans[key] = {
            "plan": plan,
            "request": "压缩完整视频到10MB以下并发在这里",
            "responses": [],
            "attempted_actions": [],
            "rounds": 0,
        }

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.review_agent_progress") as reviewer:
            response = await agent._handle_legacy(self.app, self._message("$agent confirm"))

        self.assertIs(response[0].attachment, attachment)
        reviewer.assert_not_called()
        self.assertIn("queued for delivery in this channel", platform.edited[-1][1])

    async def test_send_it_naturally_confirms_matching_pending_clip_action(self):
        attachment = BotAttachment(path="/tmp/clip.mp4")
        clips = StubFeature([BotResponse(text="clip ready", attachment=attachment)])
        agent = AgentFeature(
            StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips
        )
        plan = [{"tool": "manage_pending_clips", "arguments": {"action": "confirm"}}]
        agent.pending_plans["channel:user"] = {
            "plan": plan,
            "request": "牺牲清晰度压缩到10MB以下保证整体全部发出来",
            "responses": [BotResponse(text="fitted preview")],
            "attempted_actions": [
                {"tool": "clip_video", "arguments": {"url": "https://youtu.be/example"}},
                {"tool": "adjust_pending_clip", "arguments": {"action": "fit_discord"}},
            ],
            "rounds": 1,
        }

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.review_agent_progress") as reviewer:
            response = await agent._handle_legacy(self.app, self._message("$agent 请直接发送"))

        self.assertEqual(len(response), 1)
        self.assertIs(response[0].attachment, attachment)
        self.assertNotIn("$agent confirm", response[0].text or "")
        self.assertNotIn("fitted preview", response[0].text or "")
        self.assertNotIn("channel:user", agent.pending_plans)
        reviewer.assert_not_called()

    async def test_matching_write_plan_without_approval_language_stays_pending(self):
        clips = StubFeature([BotResponse(text="clip ready")])
        agent = AgentFeature(
            StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips
        )
        plan = [{"tool": "manage_pending_clips", "arguments": {"action": "confirm"}}]
        agent.pending_plans["channel:user"] = {
            "plan": plan,
            "request": "compress and send this video",
            "responses": [],
            "attempted_actions": [],
            "rounds": 0,
        }

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await agent._handle_legacy(self.app, self._message("$agent what is happening?"))

        self.assertIn("$agent confirm", response[-1].text)
        self.assertEqual(clips.messages, [])

    async def test_focused_answer_resumes_original_goal_with_verified_context(self):
        plans = [
            [{"tool": "list_reminders", "arguments": {}}],
            [{"tool": "current_time", "arguments": {}}],
        ]
        reviews = [
            {
                "status": "clarify",
                "summary": "I found the reminder, but its timezone is ambiguous.",
                "actions": [],
                "question": "Which timezone should I use?",
            },
            {
                "status": "clarify",
                "summary": "I now have the time, but need the desired alert offset.",
                "actions": [],
                "question": "How early should I alert you?",
            },
        ]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch(
                    "features.feature_agent.unified_chat.plan_agent_actions",
                    side_effect=plans,
                ) as planner, patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    side_effect=reviews,
                ):
            first = await self.agent._handle_legacy(self.app, self._message("$agent when is my reminder?"))
            second = await self.agent._handle_legacy(self.app, self._message("$agent use UTC"))

        self.assertIn("Which timezone should I use?", first[-1].text)
        self.assertIn("How early should I alert you?", second[-1].text)
        resumed_request = planner.call_args_list[1].args[0]
        self.assertIn("Original goal: when is my reminder?", resumed_request)
        self.assertIn("Pending question: Which timezone should I use?", resumed_request)
        self.assertIn("User's answer to the pending question: use UTC", resumed_request)
        self.assertIn("pending reminders", resumed_request)

    async def test_chinese_roll_call_returns_four_teammates_without_provider_calls(self):
        message = self._message("$agent 报数")

        with patch("features.feature_agent.unified_chat.plan_agent_actions") as planner, \
                patch("features.feature_agent.collaborative_agent.consult_luna") as luna, \
            patch("features.feature_agent.collaborative_agent.consult_gemini") as gemini, \
            patch("features.feature_agent.collaborative_agent.consult_claude") as claude:
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([item.text for item in response], [
            "**DeepSeek**\n1！我在这儿，主人。",
            "**Luna**\n2。已就位。",
            "**Gemini**\n3！研究席在线。",
            "**Claude**\n4。旁听席准备好了。",
        ])
        planner.assert_not_called()
        luna.assert_not_called()
        gemini.assert_not_called()
        claude.assert_not_called()

    async def test_roll_call_trace_names_all_four_teammates(self):
        platform = StubProgressPlatform()
        self.app.platform = platform

        response = await self.agent._handle_legacy(self.app, self._message("$agent --trace roll call"))

        self.assertEqual(len(response), 4)
        self.assertIn("1! Present", response[0].text)
        self.assertIn("2. Ready", response[1].text)
        self.assertIn("3! Research", response[2].text)
        self.assertIn("4. Listening", response[3].text)
        self.assertEqual(platform.deleted, [])
        trace = platform.edited[-1][1]
        self.assertIn("DeepSeek checked in as number 1", trace)
        self.assertIn("Luna checked in as number 2", trace)
        self.assertIn("Gemini checked in as number 3", trace)
        self.assertIn("Claude checked in as number 4", trace)

    async def test_trace_labels_real_teammate_consultations(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [
            {"tool": "consult_luna", "arguments": {"request": "coordinate this"}},
            {"tool": "consult_gemini", "arguments": {"request": "research this"}},
        ]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="Luna advice") as luna, \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="Gemini advice") as gemini, \
                patch("features.feature_agent.collaborative_agent.consult_deepseek", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
                    side_effect=["Luna, can you coordinate this?", "Gemini, can you research this?"],
                ), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save_exchange, \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Whale opening") as opening:
            message = self._message("$agent --trace Luna and Gemini, identify this car")
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([item.text for item in response], [
            "**DeepSeek**\nWhale opening",
            "**Luna**\nLuna advice",
            "**Gemini**\nGemini advice",
        ])
        save_exchange.assert_called_once()
        self.assertIn("DeepSeek: Whale opening", luna.call_args.args[0])
        self.assertIn("Luna: Luna advice", gemini.call_args.args[0])
        opening_context = opening.call_args_list[0].args[4]
        self.assertIn("已经安排单独交接给：Luna, Gemini", opening_context)
        self.assertIn("<<ASK:name>>", opening_context)
        self.assertIn("conversation 时从一个有用的观察或问题开始", opening_context)
        self.assertIn("不要代替队友回答", opening_context)
        trace = platform.edited[-1][1]
        self.assertIn("DeepSeek is inviting Luna into the room", trace)
        self.assertIn("Luna is listening to the room", trace)
        self.assertIn("Luna joined the conversation", trace)
        self.assertIn("DeepSeek is inviting Gemini into the room", trace)
        self.assertIn("Gemini is listening to the room", trace)
        self.assertIn("Gemini joined the conversation", trace)

    def test_team_turn_labels_are_not_duplicated(self):
        self.assertEqual(
            self.agent._clean_team_turn("Luna", "**Luna:** No, that is not entirely correct."),
            "No, that is not entirely correct.",
        )

    def test_standalone_repeated_team_headings_are_removed(self):
        self.assertEqual(
            self.agent._clean_team_turn(
                "DeepSeek",
                "**DeepSeek**\n**DeepSeek**\n\n```text\nA public aside.\n```\nThe answer.",
            ),
            "```text\nA public aside.\n```\nThe answer.",
        )

    def test_thought_bubble_with_skip_hides_skip_and_has_no_spoken_text(self):
        display, spoken = self.agent._parse_team_turn(
            "Claude",
            "```text\nDeepSeek 已经说完整了。\n```\n\nSKIP",
        )

        self.assertEqual(display, "```text\nDeepSeek 已经说完整了。\n```")
        self.assertEqual(spoken, "")

    def test_trailing_skip_after_spoken_turn_is_hidden(self):
        display, spoken = self.agent._parse_team_turn(
            "Claude",
            "```text\nOne last check.\n```\nA concise correction.\n\nSKIP",
        )

        self.assertEqual(display, "```text\nOne last check.\n```\nA concise correction.")
        self.assertEqual(spoken, "A concise correction.")

    def test_unfinished_reasoning_fence_is_closed_and_not_treated_as_spoken(self):
        display, spoken = self.agent._parse_team_turn(
            "Gemini",
            "```text\nI need to verify the boundary condition.",
        )

        self.assertEqual(
            display,
            "```text\nI need to verify the boundary condition.\n```",
        )
        self.assertEqual(spoken, "")

    async def test_live_reasoning_is_separate_and_deleted_without_trace(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        responses = []

        await self.agent._emit_room_turn(
            self.app,
            self._message("$agent explain this"),
            BotResponse(
                text=(
                    "**Gemini**\n```text\nCheck the limiting cases first.\n```\n"
                    "The complete answer follows."
                )
            ),
            responses,
        )

        self.assertEqual([content for _, content in platform.sent], [
            "**Gemini reasoning**\n```text\nCheck the limiting cases first.\n```",
            "**Gemini**\nThe complete answer follows.",
        ])
        self.assertEqual(platform.deleted, [platform.sent[0][1]])
        self.assertEqual(
            [response.text for response in responses],
            ["**Gemini**\nThe complete answer follows."],
        )
        self.assertTrue(responses[0].metadata["sent_live"])

    async def test_live_reasoning_is_retained_with_trace(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        responses = []

        await self.agent._emit_room_turn(
            self.app,
            self._message("$agent --trace explain this"),
            BotResponse(
                text="**Claude**\n```text\nAudit the hidden assumption.\n```\nThe assumption matters."
            ),
            responses,
            preserve_reasoning=True,
        )

        self.assertEqual(platform.deleted, [])
        self.assertEqual([response.text for response in responses], [
            "**Claude reasoning**\n```text\nAudit the hidden assumption.\n```",
            "**Claude**\nThe assumption matters.",
        ])
        self.assertTrue(all(response.metadata["sent_live"] for response in responses))

    async def test_live_room_formula_preserves_text_image_text_order(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        responses = []

        await self.agent._emit_room_turn(
            self.app,
            self._message("$agent explain matching"),
            BotResponse(
                text="**Gemini**\nBefore the equation.\n\n$$Z_L=Z_S^*$$\n\nAfter the equation."
            ),
            responses,
        )

        math_response = platform.sent_responses[1]
        self.addCleanup(lambda: Path(math_response.attachment.path).unlink(missing_ok=True))
        self.assertEqual(
            [response.text for response in platform.sent_responses],
            ["**Gemini**\nBefore the equation.\n\n", None, "\n\nAfter the equation."],
        )
        self.assertTrue(math_response.metadata["rendered_math"])
        self.assertTrue(all(response.metadata["sent_live"] for response in responses))

    async def test_trace_flag_retains_opening_reasoning_through_room_loop(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent --trace explain this")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch(
                    "features.feature_agent.unified_chat.plan_agent_actions",
                    return_value=AgentPlan([], response_depth="concise"),
                ), patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="```text\nChoose the simplest valid model.\n```\nComplete answer.",
                ), patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_claude", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(platform.deleted, [])
        self.assertEqual([response.text for response in responses], [
            "**DeepSeek reasoning**\n```text\nChoose the simplest valid model.\n```",
            "**DeepSeek**\nComplete answer.",
        ])

    async def test_legacy_asides_render_for_selected_room_members(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent compare the interacting causes")
        plan = AgentPlan(
            [],
            response_depth="detailed",
            response_mode="deliverable",
            participants=["Luna", "Gemini", "Claude"],
            approach="Compare mechanisms and limits.",
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="```text\nFrame the central distinction.\n```\nMain answer.",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    return_value="```text\nOrganize the shared conclusion.\n```\nLuna review.",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="```text\nCheck the technical boundary.\n```\nGemini review.",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    return_value="```text\nAudit the hidden assumption.\n```\nClaude review.",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([content for _, content in platform.sent], [
            "**DeepSeek reasoning**\n```text\nFrame the central distinction.\n```",
            "**DeepSeek**\nMain answer.",
            "**Luna reasoning**\n```text\nOrganize the shared conclusion.\n```",
            "**Luna**\nLuna review.",
            "**Gemini reasoning**\n```text\nCheck the technical boundary.\n```",
            "**Gemini**\nGemini review.",
            "**Claude reasoning**\n```text\nAudit the hidden assumption.\n```",
            "**Claude**\nClaude review.",
        ])
        self.assertEqual(platform.deleted, [
            platform.sent[0][1],
            platform.sent[2][1],
            platform.sent[4][1],
            platform.sent[6][1],
        ])
        self.assertEqual([response.text for response in responses], [
            "**DeepSeek**\nMain answer.",
            "**Luna**\nLuna review.",
            "**Gemini**\nGemini review.",
            "**Claude**\nClaude review.",
        ])

    def test_room_allows_three_discussion_rounds(self):
        self.assertEqual(self.agent.MAX_ROOM_DISCUSSION_ROUNDS, 3)

    def test_opening_handoff_markers_are_hidden_and_deduplicated(self):
        text, targets = self.agent._extract_opening_handoffs(
            "Let me ask Luna and Gemini. <<ASK:Luna>><<ASK:Gemini>><<ASK:Luna>>"
        )

        self.assertEqual(text, "Let me ask Luna and Gemini.")
        self.assertEqual(targets, ["Luna", "Gemini"])

    def test_discussion_reply_markers_are_hidden_and_deduplicated(self):
        text, targets = self.agent._extract_discussion_handoffs(
            "That changes the conclusion. <<REPLY:DeepSeek>><<REPLY:deepseek>>"
        )

        self.assertEqual(text, "That changes the conclusion.")
        self.assertEqual(targets, ["DeepSeek"])

    def test_grounding_request_markers_are_hidden_and_deduplicated(self):
        text, queries = self.agent._extract_grounding_requests(
            "I should verify this. <<SEARCH: latest Python release >>\n"
            "<<SEARCH:latest Python release>>"
        )

        self.assertEqual(text, "I should verify this.")
        self.assertEqual(queries, ["latest Python release"])

    def test_haiku_handoff_alias_maps_to_claude(self):
        text, targets = self.agent._extract_opening_handoffs(
            "I will ask Haiku about another interpretation."
        )

        self.assertEqual(text, "I will ask Haiku about another interpretation.")
        self.assertEqual(targets, ["Claude"])

    async def test_claude_overhears_and_joins_with_public_aside(self):
        record = {
            "id": 3,
            "channel_id": "channel",
            "message_id": "100",
            "attachment_id": "300",
            "author_id": "user",
            "url": "https://cdn.discordapp.com/car.jpg",
            "filename": "car.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
            "created_at": "2026-09-11T00:00:00+00:00",
        }
        image = {**record, "path": "/tmp/car.jpg"}
        message = self._message("$agent identify this car")
        claude_turn = "```text\nThat badge seems familiar... wait, the arches matter more.\n```\nIt resembles an Audi Quattro."
        plan = AgentPlan([{"tool": "read_attachment", "arguments": {"attachment": "3"}}], participants=["Claude"])

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.db.list_conversation_attachments", return_value=[record]), \
                patch("features.feature_agent.db.find_conversation_attachment", return_value=record), \
                patch("features.feature_agent.attachment_store.read_attachment", return_value={
                    "id": 3, "filename": "car.jpg", "content_type": "image/jpeg",
                    "size": 1024, "cached_path": image["path"], "text": "", "image": image,
                }), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.review_agent_progress", return_value={
                    "status": "complete", "summary": "The image is ready.",
                    "actions": [], "question": "",
                }), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="It may be an Audi."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    side_effect=[claude_turn, "SKIP"],
                ) as claude, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(responses[1].text, "**Claude**\nIt resembles an Audi Quattro.")
        self.assertEqual(claude.call_args_list[0].args[1], [image])
        self.assertIn("目前对话：\nDeepSeek: It may be an Audi.", claude.call_args_list[0].args[0])

    async def test_claude_thought_only_does_not_address_deepseek(self):
        message = self._message("$agent explain this briefly")
        claude_thought = "```text\nDeepSeek 已经答完整了，人家不用补充。\n```\n\nSKIP"

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Brief answer."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    return_value=claude_thought,
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ) as deepseek, patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([response.text for response in responses], [
            "**DeepSeek**\nBrief answer.",
        ])
        self.assertNotIn("\nSKIP", "\n".join(response.text or "" for response in responses))
        deepseek.assert_not_called()

    async def test_read_only_teammate_failure_does_not_stop_other_teammates(self):
        plan = [
            {"tool": "consult_gemini", "arguments": {"request": "research this"}},
            {"tool": "consult_luna", "arguments": {"request": "review this"}},
        ]
        platform = StubProgressPlatform()
        self.app.platform = platform
        message = self._message("$agent team help")
        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Whale continued"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", side_effect=RuntimeError("quota")), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="Luna continued"), \
                patch("features.feature_agent.collaborative_agent.consult_deepseek", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
                    side_effect=["Luna, can you review this?", "Gemini, can you research this?"],
                ), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(response[0].text, "**DeepSeek**\nWhale continued")
        self.assertEqual(response[1].text, "**Luna**\nLuna continued")
        self.assertNotIn("**Gemini**", "\n".join(item.text or "" for item in response))
        self.assertIn("Gemini could not join this turn", response[-1].text)
        self.assertTrue(any("Gemini could not join" in content for _, content in platform.edited))

    async def test_overhearing_teammates_skip_when_nothing_needs_adding(self):
        message = self._message("$agent hello")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=AgentPlan([], response_mode="deliverable", participants=["Luna", "Gemini"])), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Hello, Master."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP") as luna, \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="skip") as gemini, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save_exchange:
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([item.text for item in responses], ["**DeepSeek**\nHello, Master."])
        self.assertIn("目前对话：\nDeepSeek: Hello, Master.", luna.call_args.args[0])
        self.assertIn("目前对话：\nDeepSeek: Hello, Master.", gemini.call_args.args[0])
        self.assertIn("Configured IANA timezone: UTC", luna.call_args.args[2])
        self.assertIn("Configured IANA timezone: UTC", gemini.call_args.args[2])
        save_exchange.assert_called_once()

    async def test_deepseek_searches_before_showing_uncertain_opening(self):
        message = self._message("$agent what is the latest Python release?")
        grounding = SimpleNamespace(
            text="Python 3.15 was released today.",
            sources=[{"title": "Python", "url": "https://python.org", "provider": "Google Search"}],
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    side_effect=[
                        "Let me verify that. <<SEARCH:latest Python release>>",
                        "Python 3.15 is the latest release [1].",
                    ],
                ) as query_chat, patch(
                    "features.feature_agent.collaborative_agent.ground_with_google",
                    return_value=grounding,
                ) as ground, patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["SKIP", "The claim matches the supplied source."],
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(query_chat.call_count, 2)
        ground.assert_called_once_with("latest Python release", "search")
        self.assertIn("Python 3.15 is the latest release", responses[0].text)
        self.assertNotIn("<<SEARCH:", "\n".join(response.text or "" for response in responses))
        self.assertIn("https://python.org", responses[-1].text)

    async def test_gemini_can_trigger_grounded_search_during_discussion(self):
        message = self._message("$agent explain the recent release")
        grounding = SimpleNamespace(
            text="The project released version 4.2.",
            sources=[{"title": "Release", "url": "https://example.com/release", "provider": "Google Search"}],
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.collaborative_agent.consult_luna", side_effect=[
                    "SKIP",
                    "SKIP",
                    "The room's correction matches the source.",
                ]), patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    side_effect=[
                        "I need to verify the version. <<SEARCH:project latest release>>",
                        "The grounded release is version 4.2 [1].",
                        "SKIP",
                    ],
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.ground_with_google",
                    return_value=grounding,
                ) as ground, patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app,
                message,
                "explain the recent release",
                "I think it may be version 4.1.",
                "No external tool evidence was available.",
                [],
                [],
                [],
                None,
                None,
                [],
                AgentTask("explain the recent release", participants=["Gemini"]),
            )

        ground.assert_called_once_with("project latest release", "search")
        self.assertGreaterEqual(gemini.call_count, 2)
        visible = "\n".join(response.text or "" for response in responses)
        self.assertIn("The grounded release is version 4.2", visible)
        self.assertNotIn("<<SEARCH:", visible)
        self.assertIn("https://example.com/release", visible)

    async def test_room_members_receive_shared_completed_tool_state(self):
        message = self._message("$agent remind me in one hour")
        task = AgentTask(
            "remind me in one hour",
            capabilities=self.agent.registry.schemas(),
            participants=["Luna"],
        )
        task.record(
            "tool.succeeded",
            "orchestrator",
            tool="create_reminder",
            result="Reminder 42 scheduled for 2026-09-15 01:00 PDT: microwave",
        )

        with patch(
            "features.feature_agent.collaborative_agent.consult_luna",
            return_value="SKIP",
        ) as luna, patch(
            "features.feature_agent.collaborative_agent.consult_gemini",
            return_value="SKIP",
        ), patch(
            "features.feature_agent.collaborative_agent.consult_claude",
            return_value="SKIP",
        ), patch(
            "features.feature_agent.collaborative_agent.consult_deepseek",
            return_value="SKIP",
        ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.agent._continue_room_conversation(
                self.app,
                message,
                "remind me in one hour",
                "The reminder has been created.",
                "Reminder evidence is available.",
                [],
                [],
                [],
                None,
                None,
                [],
                task,
            )

        luna_context = luna.call_args.args[0]
        self.assertIn('"type": "tool.succeeded"', luna_context)
        self.assertIn('"tool": "create_reminder"', luna_context)
        self.assertIn("Reminder 42 scheduled for 2026-09-15 01:00 PDT", luna_context)

    async def test_room_member_can_use_shared_tool_and_resume_with_result(self):
        message = self._message("$agent check my reminders")
        task = AgentTask(
            "check my reminders",
            capabilities=self.agent.registry.schemas(),
        )

        with patch(
            "features.feature_agent.collaborative_agent.consult_luna",
            return_value="SKIP",
        ), patch(
            "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
            return_value="Gemini, please verify the reminder list.",
        ), patch(
            "features.feature_agent.collaborative_agent.consult_gemini",
            side_effect=[
                '<tool_calls>[{"tool":"list_reminders","arguments":{}}]</tool_calls>',
                "I checked the shared result: there are pending reminders.",
                "SKIP",
            ],
        ) as gemini, patch(
            "features.feature_agent.collaborative_agent.consult_claude",
            return_value="SKIP",
        ), patch(
            "features.feature_agent.collaborative_agent.consult_deepseek",
            return_value="SKIP",
        ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app,
                message,
                "check my reminders",
                "Gemini, could you verify the reminder list?",
                "No tool result yet.",
                [{"name": "Gemini", "request": "verify the reminder list"}],
                [],
                [],
                None,
                None,
                [],
                task,
            )

        resumed_context = gemini.call_args_list[1].args[0]
        self.assertIn('"type": "tool.succeeded"', resumed_context)
        self.assertIn('"tool": "list_reminders"', resumed_context)
        self.assertIn("pending reminders", resumed_context)
        self.assertIn(
            "I checked the shared result",
            "\n".join(response.text or "" for response in responses),
        )

    async def test_room_image_search_is_shared_with_requester_and_later_agents(self):
        message = self._message("$agent explain a buck converter")
        task = AgentTask(
            "explain a buck converter",
            capabilities=self.agent.registry.schemas(),
            response_mode="deliverable", participants=["Luna", "Gemini"],
        )
        located = SimpleNamespace(
            path="/tmp/buck-reference.png",
            filename="buck-reference.png",
            mime_type="image/png",
            title="Buck converter current paths",
            source_url="https://example.com/buck",
            image_url="https://images.example/buck.png",
        )

        with patch(
            "features.feature_agent.collaborative_agent.consult_luna",
            side_effect=[
                '<tool_calls>[{"tool":"image_search","arguments":{"query":"buck converter current paths"}}]</tool_calls>',
                "The diagram makes the switch states easier to compare.",
                "The attributed source matches the room's use of the image.",
            ],
        ) as luna, patch(
            "features.feature_agent.image_search.locate_image",
            return_value=located,
        ), patch(
            "services.tool_outcome.Path.is_file", return_value=True,
        ), patch(
            "features.feature_agent.Path.stat",
            return_value=SimpleNamespace(st_size=2048),
        ), patch(
            "features.feature_agent.collaborative_agent.consult_gemini",
            return_value="The visual confirms the inductor-current path.",
        ) as gemini, patch(
            "features.feature_agent.collaborative_agent.consult_claude",
            return_value="SKIP",
        ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app,
                message,
                "explain a buck converter",
                "Main explanation.",
                "No tool result yet.",
                [],
                [],
                [],
                None,
                None,
                [],
                task,
            )

        shared_image = luna.call_args_list[1].args[1][0]
        self.assertEqual(shared_image["path"], located.path)
        self.assertEqual(gemini.call_args.args[1][0]["path"], located.path)
        image_response = next(response for response in responses if response.attachment is not None)
        self.assertTrue(image_response.metadata["supplementary_image"])
        self.assertEqual(image_response.metadata["sources"][0]["url"], located.source_url)

    async def test_supplementary_image_attachment_continues_to_synthesis(self):
        message = self._message("$agent explain with a useful diagram")
        task = AgentTask("explain with a useful diagram", capabilities=self.agent.registry.schemas())
        image_response = BotResponse(
            text="Located diagram.",
            attachment=BotAttachment("/tmp/reference.png", "reference.png", True),
            metadata={"supplementary_image": True},
        )

        with patch.object(self.agent, "_execute_plan", return_value=[image_response]), \
                patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    return_value={
                        "status": "complete",
                        "summary": "The useful diagram is ready for synthesis.",
                        "actions": [],
                        "question": "",
                    },
                ):
            responses, stop = await self.agent._execute_goal_plan(
                self.app,
                message,
                "explain with a useful diagram",
                [{"tool": "image_search", "arguments": {"query": "diagram"}}],
                "channel:user",
                task=task,
            )

        self.assertFalse(stop)
        self.assertEqual(responses, [image_response])

    async def test_room_member_write_tool_uses_confirmation_gate(self):
        message = self._message("$agent remind me in one hour")
        task = AgentTask(
            "remind me in one hour",
            capabilities=self.agent.registry.schemas(),
        )
        tool_call = (
            '<tool_calls>[{"tool":"create_reminder","arguments":'
            '{"message":"microwave","schedule_type":"relative",'
            '"delay_value":1,"delay_unit":"hours"}}]</tool_calls>'
        )

        with patch(
            "features.feature_agent.collaborative_agent.consult_luna",
            return_value=tool_call,
        ), patch(
            "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
            return_value="Luna, please create the reminder.",
        ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app,
                message,
                "remind me in one hour",
                "Luna, please handle the reminder.",
                "No tool result yet.",
                [{"name": "Luna", "request": "create the reminder"}],
                [],
                [],
                None,
                None,
                [],
                task,
            )

        self.assertIn("need your approval", responses[-1].text)
        self.assertIn("create_reminder", responses[-1].text)
        self.assertIn("channel:user", self.agent.pending_plans)
        self.assertEqual(task.status, "awaiting_confirmation")
        self.assertEqual(self.agent.time_feature.reminder_requests, [])

    async def test_live_room_sends_opening_before_low_effort_discussion(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent discuss whether this claim is correct")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=AgentPlan([], response_mode="deliverable", participants=["Luna", "Gemini"])), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Here is my considered answer.",
                ) as opening, patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["I agree with most of that.", "Gemini, I still disagree with your correction.", "SKIP"],
                ) as luna, patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    side_effect=["Luna, that detail is not correct. <<REPLY:DeepSeek>>", "SKIP", "SKIP"],
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    side_effect=["Gemini's correction changes one part of my answer. <<REPLY:Luna>>", "SKIP", "SKIP"],
                ) as deepseek, patch(
                    "features.feature_agent.collaborative_agent.save_team_exchange"
                ) as save_exchange:
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([content for _, content in platform.sent], [
            "**DeepSeek**\nHere is my considered answer.",
            "**Luna**\nI agree with most of that.",
            "**Gemini**\nLuna, that detail is not correct.",
            "**DeepSeek**\nGemini's correction changes one part of my answer.",
            "**Luna**\nGemini, I still disagree with your correction.",
        ])
        self.assertTrue(all(response.metadata.get("sent_live") for response in responses))
        self.assertEqual(opening.call_args.args[-1], "high")
        self.assertIn("Luna: I agree with most of that.", gemini.call_args_list[0].args[0])
        self.assertIn("Gemini: Luna, that detail is not correct.", deepseek.call_args_list[0].args[0])
        self.assertIn(
            "DeepSeek: Gemini's correction changes one part of my answer.",
            luna.call_args_list[1].args[0],
        )
        self.assertNotIn("<<REPLY:", "\n".join(content for _, content in platform.sent))
        save_exchange.assert_called_once()

    async def test_semantic_response_depth_reaches_every_visible_room_provider(self):
        message = self._message("$agent provide a detailed technical explanation")
        plan = AgentPlan([], response_depth="detailed", response_mode="deliverable", participants=["Luna", "Gemini", "Claude"])

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Detailed opening.",
                ) as opening, patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    return_value="SKIP",
                ) as luna, patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="SKIP",
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    return_value="SKIP",
                ) as claude, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ) as deepseek, patch(
                    "features.feature_agent.collaborative_agent.save_team_exchange"
                ):
            await self.agent._handle_legacy(self.app, message)
            await self.agent._consult_room_member(
                "DeepSeek",
                "Continue the discussion",
                response_depth="concise",
            )

        self.assertEqual(opening.call_args.kwargs["response_depth"], "detailed")
        self.assertIn("Response depth: detailed", opening.call_args.args[4])
        for provider in (luna, gemini, claude, deepseek):
            self.assertEqual(provider.call_args.kwargs["response_depth"], "concise")
        for provider in (luna, gemini, claude):
            request = provider.call_args.args[0]
            self.assertNotIn("本轮是否必须贡献一个独立观点：是", request)
            self.assertNotIn("若本轮标记为必须贡献，则不得返回 SKIP", request)

    async def test_deliverable_room_uses_selected_perspectives(self):
        message = self._message("$agent analyze this difficult circuit")
        plan = AgentPlan([], response_depth="detailed", response_mode="deliverable", participants=["Gemini", "Claude"])

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Main circuit analysis.",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["One coordination point.", "SKIP"],
                ) as luna, patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    side_effect=["One evidence point.", "SKIP"],
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    side_effect=["One assumption to check.", "SKIP"],
                ) as claude, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.save_team_exchange"
                ):
            responses = await self.agent._handle_legacy(self.app, message)

        visible = "\n".join(response.text or "" for response in responses)
        self.assertIn("**Gemini**\nOne evidence point.", visible)
        self.assertIn("**Claude**\nOne assumption to check.", visible)
        luna.assert_not_called()
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual(claude.call_count, 1)

    async def test_complexity_and_approach_do_not_force_specialist_pass(self):
        message = self._message("$agent compare the interacting causes")
        plan = AgentPlan(
            [],
            response_depth="standard",
            approach="Compare the interacting mechanisms and practical limits.",
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Main answer."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="Luna review."), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="Gemini review.",
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    return_value="Claude review.",
                ) as claude, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ) as deepseek, patch(
                    "features.feature_agent.collaborative_agent.save_team_exchange"
                ):
            responses = await self.agent._handle_legacy(self.app, message)

        visible = "\n".join(response.text or "" for response in responses)
        self.assertEqual(visible, "**DeepSeek**\nMain answer.")
        self.assertNotIn("review.", visible)
        gemini.assert_not_called()
        claude.assert_not_called()
        deepseek.assert_not_called()

    async def test_passive_listener_failure_stays_out_of_visible_reply(self):
        message = self._message("$agent hello")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Hello, Master."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    side_effect=RuntimeError("quota"),
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([item.text for item in responses], ["**DeepSeek**\nHello, Master."])

    async def test_deepseek_asks_luna_in_the_opening_without_redundant_handoff(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent explain this unfamiliar topic")

        def luna_reply(_request, _images=None, _runtime_context=None, response_depth="standard", **options):
            if any(content.startswith("**Luna**") for _, content in platform.sent):
                return "SKIP"
            self.assertEqual([content for _, content in platform.sent], [
                "**DeepSeek**\nLuna, can you explain this topic to us?",
            ])
            return "Of course. Here is what it means."

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Luna, can you explain this topic to us? <<ASK:Luna>>",
                ), patch(
                    "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
                    return_value="Luna, can you explain this topic to us?",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=luna_reply,
                ) as luna, patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    return_value="SKIP",
                ), patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(len(responses), 2)
        self.assertEqual(
            responses[0].text,
            "**DeepSeek**\nLuna, can you explain this topic to us?",
        )
        self.assertEqual(responses[1].text, "**Luna**\nOf course. Here is what it means.")
        self.assertTrue(all(response.metadata.get("sent_live") for response in responses))
        self.assertNotIn("<<ASK:", "\n".join(response.text or "" for response in responses))
        self.assertIn("直接点名你", luna.call_args.args[0])

    async def test_deepseek_asks_claude_in_the_opening_without_redundant_handoff(self):
        message = self._message("$agent compare these interpretations")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Claude, which assumption changes the conclusion? <<ASK:Claude>>",
                ), patch(
                    "features.feature_agent.collaborative_agent.compose_deepseek_handoff",
                    return_value="Claude, which assumption changes the conclusion?",
                ), patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_claude",
                    side_effect=["The second assumption changes it.", "SKIP"],
                ) as claude, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    return_value="SKIP",
                ), patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, message)

        self.assertEqual([response.text for response in responses], [
            "**DeepSeek**\nClaude, which assumption changes the conclusion?",
            "**Claude**\nThe second assumption changes it.",
        ])
        self.assertIn("DeepSeek: Claude, which assumption changes the conclusion?", claude.call_args.args[0])
        self.assertNotIn("<<ASK:", "\n".join(response.text or "" for response in responses))

    async def test_trace_preserves_generated_process_notes_separately(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        message = self._message("$agent --trace show my reminders")
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_agent.unified_chat.generate_process_summary",
                    return_value="我先检查了提醒列表。\n等等，结果为空，所以不需要继续查。",
                ) as generate:
            response = await self.agent._handle_legacy(self.app, message)

        self.assertEqual(len(response), 1)
        self.assertIn("pending reminders", response[0].text)
        self.assertEqual(platform.deleted, [])
        self.assertIn("**Process notes (live)**", platform.edited[-1][1])
        self.assertIn("等等", platform.edited[-1][1])
        self.assertGreaterEqual(len(platform.edited), 5)
        trace_lengths = [len(content.splitlines()) for _, content in platform.edited]
        self.assertEqual(trace_lengths, sorted(trace_lengths))
        self.assertEqual(generate.call_args.args[0], "show my reminders")

    def test_trace_content_stays_within_discord_message_limit(self):
        content = self.agent._format_trace(["x" * 240 for _ in range(20)])

        self.assertLessEqual(len(content), 2000)
        self.assertIn("20. ", content)

    async def test_executes_read_only_plan_immediately(self):
        plan = [{"tool": "system_status", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent._handle_legacy(self.app, self._message("$agent show status"))

        self.assertIn("$system", response[0].text)

    async def test_web_search_returns_synthesized_answer_without_tool_wrapper(self):
        search = StubSearchFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), search, StubFeature())
        message = self._message("$agent what changed in DeepSeek today?")
        plan = [{"tool": "web_search", "arguments": {"query": "DeepSeek latest changes"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await agent._handle_legacy(self.app, message)

        self.assertEqual(
            response[0].text,
            "Synthesized current answer [1].\n\n**Sources**\n[1] Source",
        )
        self.assertNotIn("web_search", response[0].text)
        self.assertEqual(
            search.web_requests[0][1:],
            ("what changed in DeepSeek today?", ["DeepSeek latest changes"]),
        )

    async def test_multiple_search_steps_produce_one_synthesized_answer(self):
        search = StubSearchFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), search, StubFeature())
        message = self._message("$agent compare the AE86 projects and prices")
        plan = [
            {"tool": "web_search", "arguments": {"query": "Toyota AE86 BEV H2 project"}},
            {"tool": "web_search", "arguments": {"query": "TOM's Heritage AE86 price"}},
        ]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await agent._handle_legacy(self.app, message)

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0].text.count("Synthesized current answer"), 1)
        self.assertEqual(
            search.web_requests[0][1:],
            (
                "compare the AE86 projects and prices",
                ["Toyota AE86 BEV H2 project", "TOM's Heritage AE86 price"],
            ),
        )

    async def test_multiple_page_links_produce_one_synthesized_answer(self):
        search = StubSearchFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), search, StubFeature())
        message = self._message("$agent compare https://example.com/one and https://example.com/two")
        plan = [
            {"tool": "read_web_page", "arguments": {"url": "https://example.com/one"}},
            {"tool": "read_web_page", "arguments": {"url": "https://example.com/two"}},
        ]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await agent._handle_legacy(self.app, message)

        self.assertEqual(len(response), 1)
        self.assertEqual(response[0].text, "Combined page summary [1][2].")
        self.assertEqual(search.page_requests[0][2], [
            "https://example.com/one",
            "https://example.com/two",
        ])

    async def test_agent_mode_synthesizes_grounded_evidence_once_with_deepseek(self):
        search = StubSearchFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), search, StubFeature())
        message = self._message("$agent what changed today?")
        image = {
            "url": "https://cdn.discordapp.com/car.jpg",
            "filename": "car.jpg",
            "content_type": "image/jpeg",
            "size": 1024,
        }
        plan = [{"tool": "web_search", "arguments": {"query": "today changes"}}]
        grounding = SimpleNamespace(
            text="Grounded fact",
            sources=[{"title": "Source", "url": "https://example.com", "provider": "Google Search"}],
        )

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.collaborative_agent.ground_with_google", return_value=grounding), \
                patch(
                    "features.feature_agent.unified_chat.review_agent_progress",
                    return_value={
                        "status": "complete",
                        "summary": "The grounded result answers the request.",
                        "actions": [],
                        "question": "",
                    },
                ), \
                patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["SKIP", "来源与结论一致，完整清单紧随其后。"],
                ) as luna, \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="SKIP"), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Whale final [1].") as query:
            responses = await agent._handle_legacy(self.app, message)

        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[-1], "medium")
        self.assertEqual(query.call_args.args[6], [])
        self.assertEqual(responses[0].text, "**DeepSeek**\nWhale final [1].")
        self.assertIn("Google Search: Source", responses[1].text)
        self.assertTrue(responses[1].metadata["suppress_embeds"])
        luna.assert_not_called()

    async def test_write_plan_requires_confirmation(self):
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "call Alex",
                "schedule_type": "absolute",
                "remind_at": "2026-09-10T20:00:00+00:00",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await self.agent._handle_legacy(self.app, self._message("$agent schedule something for tomorrow"))

        self.assertIn("$agent confirm", preview[0].text)
        confirmed = await self.agent._handle_legacy(self.app, self._message("$agent confirm"))
        self.assertIn("scheduled", confirmed[0].text)

    async def test_relative_reminder_confirmation_keeps_original_request_time(self):
        time_feature = StubTimeFeature()
        feature = StubFeature()
        agent = AgentFeature(time_feature, feature, feature, feature)
        request_time = datetime(2026, 9, 14, 20, 41, tzinfo=timezone.utc)
        confirmation_time = datetime(2026, 9, 14, 20, 42, tzinfo=timezone.utc)
        request_message = IncomingMessage(**{
            **self.message.__dict__,
            "content": "$agent 两个小时之后提醒微波炉",
            "created_at": request_time,
        })
        confirmation_message = IncomingMessage(**{
            **self.message.__dict__,
            "content": "$agent confirm",
            "created_at": confirmation_time,
        })
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "微波炉",
                "schedule_type": "relative",
                "delay_value": 2,
                "delay_unit": "hours",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await agent._handle_legacy(self.app, request_message)
            await agent._handle_legacy(self.app, confirmation_message)

        self.assertIn('"delay_value": 2', preview[0].text)
        self.assertNotIn("\\u", preview[0].text)
        reminder_message, reminder_schedule = time_feature.reminder_requests[0]
        self.assertEqual(reminder_message.created_at, request_time)
        self.assertEqual(reminder_schedule["reminder_text"], "微波炉")
        self.assertEqual(reminder_schedule["delay_value"], 2)
        self.assertEqual(reminder_schedule["delay_unit"], "hours")

    async def test_relative_reminder_uses_configured_local_request_time(self):
        time_feature = TimeFeature()
        feature = StubFeature()
        agent = AgentFeature(time_feature, feature, feature, feature)
        self.app.config["TIMEZONE"] = "America/Los_Angeles"
        request_time = datetime(2026, 9, 14, 20, 41, tzinfo=timezone.utc)
        confirmation_time = datetime(2026, 9, 14, 20, 42, tzinfo=timezone.utc)
        request_message = IncomingMessage(**{
            **self.message.__dict__,
            "content": "$agent 两个小时之后提醒微波炉",
            "created_at": request_time,
        })
        confirmation_message = IncomingMessage(**{
            **self.message.__dict__,
            "content": "$agent confirm",
            "created_at": confirmation_time,
        })
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "微波炉",
                "schedule_type": "relative",
                "delay_value": 2,
                "delay_unit": "hours",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch.object(time_feature, "_save_reminder", return_value=42) as save_reminder:
            await agent._handle_legacy(self.app, request_message)
            response = await agent._handle_legacy(self.app, confirmation_message)

        remind_at = save_reminder.call_args.args[1]
        self.assertEqual(remind_at.strftime("%Y-%m-%d %H:%M %Z"), "2026-09-14 15:41 PDT")
        self.assertEqual(save_reminder.call_args.args[2], "微波炉")
        self.assertIn("2026-09-14 15:41:00 PDT", response[0].text)

    async def test_absolute_reminder_uses_agent_resolved_timestamp(self):
        time_feature = TimeFeature()
        feature = StubFeature()
        agent = AgentFeature(time_feature, feature, feature, feature)
        self.app.config["TIMEZONE"] = "America/Los_Angeles"
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "join the meeting",
                "schedule_type": "absolute",
                "remind_at": "2026-09-10T09:15:00-07:00",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch.object(time_feature, "_save_reminder", return_value=43) as save_reminder:
            await agent._handle_legacy(self.app, self._message("$agent remind me at quarter past nine tomorrow"))
            response = await agent._handle_legacy(self.app, self._message("$agent confirm"))

        remind_at = save_reminder.call_args.args[1]
        self.assertEqual(remind_at.strftime("%Y-%m-%d %H:%M %Z"), "2026-09-10 09:15 PDT")
        self.assertEqual(save_reminder.call_args.args[2], "join the meeting")
        self.assertIn("2026-09-10 09:15:00 PDT", response[0].text)

    async def test_rejects_mixed_reminder_schedule_before_confirmation(self):
        plan = [{
            "tool": "create_reminder",
            "arguments": {
                "message": "invalid schedule",
                "schedule_type": "relative",
                "delay_value": 2,
                "delay_unit": "hours",
                "remind_at": "2026-09-10T09:15:00-07:00",
            },
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent._handle_legacy(self.app, self._message("$agent remind me later"))

        self.assertIn("relative reminder cannot include remind_at", response[0].text)
        self.assertNotIn("$agent confirm", response[0].text)

    async def test_rejects_unregistered_planner_tool(self):
        plan = [{"tool": "run_shell", "arguments": {"command": "date"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent._handle_legacy(self.app, self._message("$agent run a command"))

        self.assertIn("Unknown tool", response[0].text)

    async def test_queue_music_uses_typed_tool_after_confirmation(self):
        music = StubFeature([BotResponse(text="added to queue")])
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), music_feature=music)
        plan = [{"tool": "queue_music", "arguments": {"url": "https://youtu.be/example"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            preview = await agent._handle_legacy(self.app, self._message("$agent add that music to the playlist"))

        self.assertIn("$agent confirm", preview[0].text)
        result = await agent._handle_legacy(self.app, self._message("$agent confirm"))
        self.assertIn("added to queue", result[0].text)
        self.assertEqual(music.messages[0].content, '$music --youtube "https://youtu.be/example" --queue')

    async def test_planner_receives_replied_message_context(self):
        message = self._message("$agent add that music to the playlist")
        message.metadata["referenced_content"] = "https://youtu.be/example"

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]) as planner, \
                patch("features.feature_agent.unified_chat.query_chat", return_value="okay"):
            await self.agent._handle_legacy(self.app, message)

        planning_request = planner.call_args.args[0]
        self.assertIn("add that music", planning_request)
        self.assertIn("Replied-to message: https://youtu.be/example", planning_request)

    async def test_clip_tool_runs_preparation_immediately_and_preserves_attachment(self):
        attachment = BotAttachment(path="/tmp/clip.mp4")
        clips = StubFeature([BotResponse(text="clip ready", attachment=attachment)])
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips)
        plan = [{
            "tool": "clip_video",
            "arguments": {"url": "https://youtu.be/example", "start": "1:05", "end": "1:15"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            result = await agent._handle_legacy(self.app, self._message("$agent clip this video for me"))

        self.assertIs(result[0].attachment, attachment)
        self.assertEqual(
            clips.messages[0].content,
            '$clip --url "https://youtu.be/example" --start "1:05" --end "1:15"',
        )

    async def test_oversized_full_video_asks_for_trim_compression_or_full_quality(self):
        agent = AgentFeature(
            StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=ClipFeature()
        )
        plan = [{"tool": "clip_video", "arguments": {"url": "https://youtu.be/example"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch(
                    "features.feature_clip.clipQuery.get_video_info",
                    return_value=("Example", "Youtube", 301),
                ):
            result = await agent._handle_legacy(self.app, self._message("$agent process this video"))

        self.assertIn("96.56MB", result[-1].text)
        self.assertIn("截取哪一段", result[-1].text)
        self.assertIn("压缩完整视频", result[-1].text)
        self.assertIn("保留当前画质", result[-1].text)

    def test_latest_fitting_clip_preview_does_not_ask_for_goal_again(self):
        oversized = BotResponse(metadata={
            "clip_preview": {
                "limit_mb": 10,
                "clips": [{
                    "estimated_size_mb": 96.56,
                    "keep_file": False,
                    "range_explicit": False,
                }],
            },
        })
        fitted = BotResponse(metadata={
            "clip_preview": {
                "limit_mb": 10,
                "clips": [{
                    "estimated_size_mb": 8.97,
                    "keep_file": False,
                    "range_explicit": False,
                }],
            },
        })

        response = self.agent._clip_goal_response(
            [oversized, fitted],
            "牺牲清晰度压缩到10MB以下保证整体全部发出来",
        )

        self.assertIsNone(response)

    async def test_ordinary_room_queries_only_selected_participant(self):
        plan = AgentPlan([], response_depth="detailed", participants=["Gemini"])
        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Getting started is rough.") as opening, \
                patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock,
                             return_value="The phone is suspiciously interesting when a textbook opens.") as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._handle_legacy(self.app, self._message("$agent I keep procrastinating"))

        self.assertEqual(opening.call_args.kwargs["response_depth"], "concise")
        self.assertEqual(opening.call_args.args[-1], "medium")
        self.assertEqual([call.args[0] for call in member.await_args_list], ["Gemini"])
        self.assertEqual(len(responses), 2)

    async def test_homemade_food_photo_reaches_all_invited_voices_without_planning_or_review(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent 烧卖你们觉得好吃吗？这个是我做的")
        image = {
            "url": "https://cdn.discordapp.com/food.jpg", "filename": "food.jpg",
            "content_type": "image/jpeg", "size": 1024,
        }
        record = {**image, "id": 42, "channel_id": "channel"}
        cached_image = {**image, "path": "/tmp/food.jpg"}
        message.metadata["attachments"] = [image]

        async def reaction(name, request, images, runtime_context, **options):
            # All explicitly invited openings use the same inspected photo.
            self.assertIn("同时开始生成", request)
            self.assertEqual(images, [cached_image])
            self.assertEqual(options["effort"], "low")
            self.assertEqual(options["request_timeout"], 35.0)
            return {"Luna": "自己做的很用心呢。", "Gemini": "给我留一颗！", "Claude": "摆盘也挺整齐。"}[name]

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.db.list_conversation_attachments", return_value=[record]), \
                patch("features.feature_agent.db.find_conversation_attachment", return_value=record), \
                patch("features.feature_agent.attachment_store.read_attachment", return_value={
                    "id": 42, "filename": "food.jpg", "content_type": "image/jpeg", "size": 1024,
                    "cached_path": cached_image["path"], "text": "", "image": cached_image,
                }) as fetch, patch("features.feature_agent.collaborative_agent.query_room_route", return_value=
                    '{"route":"conversation","participants":[],"effort":"low"}') as router, \
                patch("features.feature_agent.collaborative_agent.query_luna_control") as full_planner, \
                patch("features.feature_agent.unified_chat.review_agent_progress") as reviewer, \
                patch("features.feature_agent.unified_chat.query_chat", return_value="主人自己做的？看着很香！") as opening, \
                patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock, side_effect=reaction) as members, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            responses = await self.agent._handle_legacy(self.app, message)

        router.assert_called_once()
        full_planner.assert_not_called()
        reviewer.assert_not_called()
        fetch.assert_called_once_with(record)
        opening.assert_called_once()
        self.assertEqual(opening.call_args.args[6], [cached_image])
        self.assertEqual(opening.call_args.args[-1], "low")
        self.assertEqual(opening.call_args.kwargs["request_timeout"], 35.0)
        self.assertCountEqual([call.args[0] for call in members.await_args_list], ["Luna", "Gemini", "Claude"])
        self.assertEqual(len(responses), 4)
        self.assertTrue(all(response.metadata["sent_live"] for response in responses))
        self.assertIn("**Claude**", save.call_args.args[1])

    async def test_invited_participant_skip_is_reported_instead_of_silently_dropped(self):
        task = AgentTask("Luna学姐，你怎么看？")
        with patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock, return_value="SKIP") as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "Luna也说说吧。", "", [], [], [], None, None, [], task,
            )
        member.assert_awaited_once()
        self.assertIn("Luna could not join", responses[-1].text)

    async def test_group_openings_arrive_before_slow_deepseek_and_in_completion_order(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        names = ["DeepSeek", "Luna", "Gemini", "Claude"]
        started = {name: asyncio.Event() for name in names}
        release = {name: asyncio.Event() for name in names}
        delivered = {name: asyncio.Event() for name in names}
        release["Gemini"].set()
        original_send = platform.send_channel_response

        async def send(channel, response):
            await original_send(channel, response)
            for name in names:
                if response.text.startswith(f"**{name}**"):
                    delivered[name].set()

        platform.send_channel_response = send
        async def reply(name):
            started[name].set()
            await release[name].wait()
            return f"{name}在呢。"

        async def opening(function, *args, **kwargs):
            self.assertIs(function, unified_chat.query_chat)
            return await reply("DeepSeek")

        async def member(name, request, images, context, **kwargs):
            self.assertIn("尚未听到", request)
            self.assertNotIn("Gemini在呢", request)
            return await reply(name)

        task = AgentTask("你们在吗？", conversation_only=True, participants=names[1:])
        with patch("features.feature_agent.asyncio.to_thread", side_effect=opening), \
                patch.object(self.agent, "_consult_room_member", side_effect=member), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            work = asyncio.create_task(self.agent._parallel_conversation(
                self.app, self.message, task.goal, "", [], task, None, None))
            try:
                await asyncio.wait_for(delivered["Gemini"].wait(), 1)
                await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started.values())), 1)
                self.assertFalse(delivered["DeepSeek"].is_set())
                for name in ["Claude", "DeepSeek", "Luna"]:
                    release[name].set()
                    await asyncio.wait_for(delivered[name].wait(), 1)
                answers = await asyncio.wait_for(work, 1)
            finally:
                for event in release.values():
                    event.set()
                if not work.done():
                    work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        self.assertEqual([answer.text.split("\n", 1)[0] for answer in answers],
                         ["**Gemini**", "**Claude**", "**DeepSeek**", "**Luna**"])
        self.assertTrue(all(answer.metadata["sent_live"] for answer in answers))
        transcript = save.call_args.args[1]
        self.assertLess(transcript.index("**Gemini**"), transcript.index("**DeepSeek**"))

    async def test_parallel_member_failure_does_not_prevent_other_replies(self):
        self.app.platform = StubLiveRoomPlatform()
        task = AgentTask("你们在吗？", conversation_only=True, participants=["Luna", "Gemini", "Claude"])
        async def member(name, *args, **kwargs):
            if name == "Gemini":
                raise TimeoutError("test failure")
            return f"{name}在呢。"
        with patch("features.feature_agent.unified_chat.query_chat", return_value="在呢。"), \
                patch.object(self.agent, "_consult_room_member", side_effect=member), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            answers = await self.agent._parallel_conversation(
                self.app, self.message, task.goal, "", [], task, None, None)
        text = "\n".join(answer.text for answer in answers)
        self.assertIn("Gemini could not join", text)
        for name in ["DeepSeek", "Luna", "Claude"]:
            self.assertIn(f"**{name}**", text)
        self.assertNotIn("test failure", save.call_args.args[1])

    async def test_parallel_write_request_still_pauses_for_approval(self):
        self.app.platform = StubLiveRoomPlatform()
        task = AgentTask("你们在吗？", conversation_only=True, participants=["Luna", "Gemini", "Claude"],
                         capabilities=self.agent.registry.schemas())
        async def member(name, *args, **kwargs):
            if name == "Luna":
                return '我可以提醒你。<tool_calls>[{"tool":"create_reminder","arguments":{"message":"study","schedule_type":"relative","delay_value":2,"delay_unit":"hours"}}]</tool_calls>'
            await asyncio.Event().wait()
        with patch("features.feature_agent.unified_chat.query_chat", return_value="在呢。"), \
                patch.object(self.agent, "_consult_room_member", side_effect=member), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            answers = await asyncio.wait_for(self.agent._parallel_conversation(
                self.app, self.message, task.goal, "", [], task, None, None), 1)
        self.assertEqual(self.agent.time_feature.reminder_requests, [])
        self.assertIn("$agent confirm", answers[-1].text)
        self.assertIn("channel:user", self.agent.pending_plans)
        self.assertNotIn("tool_calls", "\n".join(answer.text for answer in answers))

    async def test_work_owner_speaks_before_tool_without_full_planner_or_reviewer(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        async def system_status(app, message):
            self.assertIn("I'll inspect the server.", platform.sent[0][1])
            return [BotResponse(text="CPU 5%; service healthy")]
        self.agent.system_feature.handle = system_status
        decisions = [
            json.dumps({"status": "continue", "message": "I'll inspect the server.",
                        "actions": [{"tool": "system_status", "arguments": {}}]}),
            json.dumps({"status": "complete", "message": "The server is healthy: CPU 5%.", "actions": []}),
        ]
        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.collaborative_agent.query_room_route", return_value=
                      '{"route":"task","participants":[],"effort":"medium"}'), \
                patch("features.feature_agent.collaborative_agent.query_luna_control") as planner, \
                patch("features.feature_agent.unified_chat.review_agent_progress") as reviewer, \
                patch("features.feature_agent.unified_chat.generate_process_summary") as summary, \
                patch("features.feature_agent.unified_chat.query_chat", side_effect=decisions) as owner:
            answers = await self.agent._handle_legacy(self.app, self._message("$agent inspect my server"))
        self.assertEqual(owner.call_count, 2)
        self.assertIn('"status": "success"', owner.call_args.args[0])
        planner.assert_not_called()
        reviewer.assert_not_called()
        summary.assert_not_called()
        self.assertTrue(all(answer.metadata["sent_live"] for answer in answers))

    async def test_download_source_failure_is_reported_without_preview_or_compression_detour(self):
        self.agent = AgentFeature(self.agent.time_feature, self.agent.system_feature,
                                  self.agent.search_feature, self.agent.help_feature, clip_feature=ClipFeature())
        decisions = [
            json.dumps({"status": "continue", "message": "I'll download the full video.",
                        "actions": [{"tool": "download_video", "arguments": {"url": "https://www.bilibili.com/video/example"}}]}),
            json.dumps({"status": "blocked", "message": "The source returned no video formats; changing bitrate cannot fix source access.",
                        "actions": []}),
        ]
        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.collaborative_agent.query_room_route", return_value=
                      '{"route":"task","participants":[],"effort":"medium"}'), \
                patch("features.feature_agent.unified_chat.query_chat", side_effect=decisions) as owner, \
                patch("features.feature_clip.clipQuery.get_video_info", side_effect=clip_query.VideoSourceError("No video formats found")), \
                patch("features.feature_clip.clipQuery.create_clip") as encode:
            answers = await self.agent._handle_legacy(self.app, self._message("$agent download this video"))
        encode.assert_not_called()
        self.assertIn('"error_code": "source_unavailable"', owner.call_args.args[0])
        self.assertIn('"status": "failed"', owner.call_args.args[0])
        task = self.agent.task_sessions["channel:user"]
        self.assertEqual(task.status, "blocked")
        self.assertIn("tool.failed", [event.event_type for event in task.events])
        self.assertNotIn("tool.succeeded", [event.event_type for event in task.events])
        self.assertIn("source returned no video formats", answers[-1].text)

    async def test_work_confirmation_resumes_once_with_original_schedule_anchor(self):
        decisions = [
            json.dumps({"status": "continue", "message": "I'll set the reminder.", "actions": [{
                "tool": "create_reminder", "arguments": {"message": "study", "schedule_type": "relative",
                                                            "delay_value": 2, "delay_unit": "hours"}}]}),
            json.dumps({"status": "complete", "message": "The reminder is scheduled.", "actions": []}),
        ]
        message = self._message("$agent remind me in two hours to study")
        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.collaborative_agent.query_room_route", return_value=
                      '{"route":"task","participants":[],"effort":"medium"}') as route, \
                patch("features.feature_agent.unified_chat.query_chat", side_effect=decisions):
            await self.agent._handle_legacy(self.app, message)
            self.assertEqual(self.agent.time_feature.reminder_requests, [])
            await self.agent._handle_legacy(self.app, self._message("$agent confirm"))
        route.assert_called_once()
        self.assertEqual(len(self.agent.time_feature.reminder_requests), 1)
        self.assertEqual(self.agent.time_feature.reminder_requests[0][0].created_at, message.created_at)
        self.assertNotIn("channel:user", self.agent.work_runs)

    async def test_characters_keep_separate_diaries_and_recall_them_without_extra_generation(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(db, "DATABASE_PATH", str(Path(directory) / "diary.db")):
            db.init_database()
            message = self._message("$agent 你们，我现在在学粤语。")
            notes = {
                "DeepSeek": "DeepSeek-private: 主人在学粤语，下次可以陪她练一句。",
                "Luna": "Luna-private: 我想温柔地支持主人的粤语学习。",
                "Gemini": "Gemini-private: 我有点好奇主人会先学哪些粤语词。",
                "Claude": "Claude-private: 主人在学粤语，我会留意她喜欢的表达。",
            }

            def with_note(name, spoken):
                return spoken + "\n<diary_updates>" + json.dumps([{
                    "key": "study.language", "kind": "fact", "note": notes[name], "importance": 4,
                }], ensure_ascii=False) + "</diary_updates>"

            async def teammate(name, request, images, runtime_context, **options):
                self.assertIn(f"You are {name}. This is your own diary", runtime_context)
                self.assertNotIn("DeepSeek-private", request + runtime_context)
                return with_note(name, f"{name}的简短回应。")

            plans = [AgentPlan([], participants=["Luna", "Gemini", "Claude"], conversation_only=True), AgentPlan([])]
            with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                    patch("features.feature_agent.unified_chat.plan_agent_actions", side_effect=plans), \
                    patch("features.feature_agent.unified_chat.query_chat",
                          side_effect=[with_note("DeepSeek", "那我们来练一句吧。"), "你在学粤语呀。"]) as opening, \
                    patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock, side_effect=teammate) as members:
                responses = await self.agent._handle_legacy(self.app, message)
                self.assertNotIn("diary_updates", "\n".join(response.text for response in responses))
                self.assertEqual(opening.call_count, 1)
                self.assertEqual(members.await_count, 3)
                for name in notes:
                    stored = db.list_diary_notes(diary_scope(message), name)
                    self.assertEqual([note["note"] for note in stored], [notes[name]])

                history = db.load_chat_history(message.channel_id, "agent")
                self.assertNotIn("private:", str(history))
                db.clear_chat_history(message.channel_id, "agent")
                await self.agent._handle_legacy(self.app, self._message("$agent 你还记得我在学什么吗？"))
                self.assertIn(notes["DeepSeek"], opening.call_args.args[4])
                self.assertNotIn(notes["Luna"], opening.call_args.args[4])
                self.assertEqual(opening.call_count, 2)
                self.assertEqual(members.await_count, 3)

    async def test_opening_question_waits_for_user_without_querying_optional_teammates(self):
        task = AgentTask("I keep procrastinating", participants=["Gemini"])
        with patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock) as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal,
                "Is starting or staying focused harder? <wait_for_user/>", "", [], [], [], None, None, [], task,
            )
        member.assert_not_awaited()
        self.assertEqual(responses[0].text, "**DeepSeek**\nIs starting or staying focused harder?")
        self.assertNotIn("wait_for_user", save.call_args.args[1])

    async def test_teammate_question_stops_a_followup_invitation(self):
        task = AgentTask("I keep procrastinating", participants=["Gemini"])
        with patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock,
                          return_value="Which subject? <wait_for_user/> <<REPLY:DeepSeek>>") as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "Getting started is rough.", "", [], [], [], None, None, [], task,
            )
        self.assertEqual(member.await_count, 1)
        self.assertEqual(responses[-1].text, "**Gemini**\nWhich subject?")
        self.assertNotIn("wait_for_user", save.call_args.args[1])
        self.assertNotIn("<<REPLY:", save.call_args.args[1])

    async def test_conversation_turn_budget_stops_teammate_ping_pong(self):
        task = AgentTask("talk about studying", participants=["Gemini"])
        with patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock, side_effect=[
            "Maybe a small first step? <<REPLY:DeepSeek>>",
            "Just open the book. <<REPLY:Gemini>>",
        ]) as member, patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "Starting feels hard.", "", [], [], [], None, None, [], task,
            )
        self.assertEqual(member.await_count, 2)
        self.assertEqual(len(responses), 3)

    async def test_explicit_group_invitation_keeps_all_requested_voices(self):
        task = AgentTask("Everyone, how was your day?", participants=["Luna", "Gemini", "Claude"])
        with patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock,
                          side_effect=["Busy!", "I survived.", "Quite pleasant."]) as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "Mine was sleepy.", "", [], [], [], None, None, [], task,
            )
        self.assertEqual([call.args[0] for call in member.await_args_list], ["Luna", "Gemini", "Claude"])
        self.assertEqual(len(responses), 4)

    async def test_teammate_receives_recent_user_answer(self):
        task = AgentTask("yes, that one", participants=["Luna"])
        with patch("features.feature_agent.conversation_context", return_value="[User]: I study physics."), \
                patch.object(self.agent, "_consult_room_member", new_callable=AsyncMock, return_value="Physics, got it.") as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "Let's start there.", "", [], [], [], None, None, [], task,
            )
        self.assertIn("[User]: I study physics.", member.await_args.args[3])

    async def test_explicit_roleplay_chat_persists_exchange(self):
        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Hello again!") as query:
            responses = await self.agent._handle_legacy(self.app, self._message("$agent hello"))
        self.assertTrue(query.call_args.args[5])
        self.assertEqual(responses[0].text, "Hello again!")

    async def test_requested_deliverable_retains_source_audit(self):
        task = AgentTask("Write a sourced report", response_mode="deliverable", response_depth="detailed")
        sources = [{"provider": "Web", "title": "Evidence", "url": "https://example.com/evidence"}]
        with patch("features.feature_agent.collaborative_agent.consult_luna",
                   return_value="The report matches the provided evidence.") as audit, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.agent._continue_room_conversation(
                self.app, self.message, task.goal, "The report [1].", "Verified evidence.",
                [], sources, [], None, None, [], task,
            )
        audit.assert_called_once()
        self.assertIn("最终来源审计回合", audit.call_args.args[0])
        self.assertEqual(responses[1].text, "**Luna**\nThe report matches the provided evidence.")
        self.assertIn("https://example.com/evidence", responses[2].text)

    def _message(self, content):
        return IncomingMessage(**{**self.message.__dict__, "content": content})


if __name__ == "__main__":
    unittest.main()

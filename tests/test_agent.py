from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.bot_types import BotAttachment, BotResponse, IncomingMessage
from features.feature_agent import AgentFeature
from features.feature_clip import ClipFeature


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

    async def send_channel_message(self, channel_id, content):
        self.sent.append((channel_id, content))
        return object()


class AgentFeatureTests(unittest.IsolatedAsyncioTestCase):
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
        response = await self.agent.handle(self.app, self.message)

        self.assertIn("system_status", response[0].text)
        self.assertIn("create_reminder", response[0].text)
        self.assertIn("read_web_page", response[0].text)

    async def test_natural_reminder_uses_registered_tool(self):
        message = self._message("$agent remind me tomorrow at this time to test")
        plan = [{"tool": "create_reminder", "arguments": {"request": "tomorrow at this time to test"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan) as planner:
            response = await self.agent.handle(self.app, message)

        planner.assert_called_once()
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
            responses = await agent.handle(
                self.app,
                self._message("$agent draw a moonlit library in anime style"),
            )

        self.assertEqual(draw.requests[0][1], "An anime-style moonlit library")
        self.assertEqual(draw.requests[0][2], {"provider": "gemini"})
        self.assertEqual(responses[0].text, "**Gemini**\nI finished the image.")
        self.assertEqual(responses[0].attachment.filename, "generated.png")
        save_exchange.assert_called_once()
        self.assertIn("Gemini", save_exchange.call_args.args[1])

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
            response = await self.agent.handle(self.app, message)

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
            response = await self.agent.handle(self.app, message)

        self.assertEqual(response[0].text, "**DeepSeek**\na cat")
        read_attachment.assert_called_once_with(record)
        self.assertEqual(query_chat.call_args.args[6], [fetched_image])

    async def test_missing_action_context_can_request_clarification(self):
        plan = [{
            "tool": "ask_clarification",
            "arguments": {"question": "Which video should I clip?"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent clip this video"))

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
            first = await self.agent.handle(
                self.app, self._message("$agent 你们查一下有没有类似的")
            )
            follow_up = self._message("$agent 刚才图片里的")
            follow_up.metadata["attachments"] = [image]
            second = await self.agent.handle(self.app, follow_up)

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
            response = await self.agent.handle(
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
            responses = await self.agent.handle(
                self.app, self._message("$agent compare attachments 1 and 2")
            )

        self.assertEqual(query_chat.call_args.args[6], fetched)
        self.assertEqual(responses[0].text, "**DeepSeek**\nTwo views.")

    async def test_progress_shows_safe_stages_and_is_removed(self):
        platform = StubProgressPlatform()
        self.app.platform = platform
        plan = [{"tool": "list_reminders", "arguments": {}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await self.agent.handle(self.app, self._message("$agent show my reminders"))

        self.assertIn("pending reminders", response[0].text)
        self.assertIn("deciding whether to invite Luna, Gemini, or Claude", platform.sent[0][1])
        self.assertTrue(any("plan: try `list reminders`" in content.lower() for _, content in platform.edited))
        self.assertTrue(any("deepseek is running list reminders" in content.lower() for _, content in platform.edited))
        self.assertEqual(platform.deleted, [platform.sent[0][2]])

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
            response = await self.agent.handle(self.app, self._message("$agent handle my reminder"))

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
            response = await self.agent.handle(self.app, self._message("$agent when is my reminder?"))

        self.assertIn("Which timezone should I use?", response[-1].text)
        self.assertEqual(reviewer.call_count, 2)
        self.assertIn("pending reminders", reviewer.call_args_list[1].args[3][0])
        self.assertTrue(any("$time" in outcome for outcome in reviewer.call_args_list[1].args[3]))
        self.assertTrue(any("Continuing with `current time`" in content for _, content in platform.edited))

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
                    "arguments": {"request": "tomorrow at 8 PM to call Alex"},
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
            preview = await self.agent.handle(self.app, self._message("$agent ensure I call Alex tomorrow"))
            confirmed = await self.agent.handle(self.app, self._message("$agent confirm"))

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
            response = await agent.handle(self.app, self._message("$agent confirm"))

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
            response = await agent.handle(self.app, self._message("$agent 请直接发送"))

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
            response = await agent.handle(self.app, self._message("$agent what is happening?"))

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
            first = await self.agent.handle(self.app, self._message("$agent when is my reminder?"))
            second = await self.agent.handle(self.app, self._message("$agent use UTC"))

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
            response = await self.agent.handle(self.app, message)

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

        response = await self.agent.handle(self.app, self._message("$agent --trace roll call"))

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
            response = await self.agent.handle(self.app, message)

        self.assertEqual([item.text for item in response], [
            "**DeepSeek**\nWhale opening",
            "**DeepSeek**\nLuna, can you coordinate this?",
            "**Luna**\nLuna advice",
            "**DeepSeek**\nGemini, can you research this?",
            "**Gemini**\nGemini advice",
        ])
        save_exchange.assert_called_once()
        self.assertIn("DeepSeek: Whale opening", luna.call_args.args[0])
        self.assertIn("Luna: Luna advice", gemini.call_args.args[0])
        opening_context = opening.call_args_list[0].args[4]
        self.assertIn("已经安排单独交接给：Luna, Gemini", opening_context)
        self.assertIn("<<ASK:name>>", opening_context)
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

    def test_room_allows_three_discussion_rounds(self):
        self.assertEqual(self.agent.MAX_ROOM_DISCUSSION_ROUNDS, 3)

    def test_opening_handoff_markers_are_hidden_and_deduplicated(self):
        text, targets = self.agent._extract_opening_handoffs(
            "Let me ask Luna and Gemini. <<ASK:Luna>><<ASK:Gemini>><<ASK:Luna>>"
        )

        self.assertEqual(text, "Let me ask Luna and Gemini.")
        self.assertEqual(targets, ["Luna", "Gemini"])

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
        plan = [{"tool": "read_attachment", "arguments": {"attachment": "3"}}]

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
            responses = await self.agent.handle(self.app, message)

        self.assertEqual(responses[1].text, f"**Claude**\n{claude_turn}")
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
            responses = await self.agent.handle(self.app, message)

        self.assertEqual([response.text for response in responses], [
            "**DeepSeek**\nBrief answer.",
            "**Claude**\n```text\nDeepSeek 已经答完整了，人家不用补充。\n```",
        ])
        self.assertNotIn("\nSKIP", "\n".join(response.text or "" for response in responses))
        self.assertIn("最近一位的口头发言：无（只有公开心声）", deepseek.call_args.args[0])
        self.assertIn("你是否被直接点名：否", deepseek.call_args.args[0])

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
            response = await self.agent.handle(self.app, message)

        self.assertEqual(response[0].text, "**DeepSeek**\nWhale continued")
        self.assertEqual(response[1].text, "**DeepSeek**\nLuna, can you review this?")
        self.assertEqual(response[2].text, "**Luna**\nLuna continued")
        self.assertNotIn("**Gemini**", "\n".join(item.text or "" for item in response))
        self.assertIn("Gemini could not join this turn", response[-1].text)
        self.assertTrue(any("Gemini could not join" in content for _, content in platform.edited))

    async def test_overhearing_teammates_skip_when_nothing_needs_adding(self):
        message = self._message("$agent hello everyone")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch("features.feature_agent.unified_chat.query_chat", return_value="Hello, Master."), \
                patch("features.feature_agent.collaborative_agent.consult_luna", return_value="SKIP") as luna, \
                patch("features.feature_agent.collaborative_agent.consult_gemini", return_value="skip") as gemini, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save_exchange:
            responses = await self.agent.handle(self.app, message)

        self.assertEqual([item.text for item in responses], ["**DeepSeek**\nHello, Master."])
        self.assertIn("目前对话：\nDeepSeek: Hello, Master.", luna.call_args.args[0])
        self.assertIn("目前对话：\nDeepSeek: Hello, Master.", gemini.call_args.args[0])
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
            responses = await self.agent.handle(self.app, message)

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
            )

        ground.assert_called_once_with("project latest release", "search")
        self.assertGreaterEqual(gemini.call_count, 2)
        visible = "\n".join(response.text or "" for response in responses)
        self.assertIn("The grounded release is version 4.2", visible)
        self.assertNotIn("<<SEARCH:", visible)
        self.assertIn("https://example.com/release", visible)

    async def test_live_room_sends_opening_before_low_effort_discussion(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent discuss whether this claim is correct")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="Here is my considered answer.",
                ) as opening, patch(
                    "features.feature_agent.collaborative_agent.consult_luna",
                    side_effect=["I agree with most of that.", "Gemini, I still disagree with your correction.", "SKIP"],
                ) as luna, patch(
                    "features.feature_agent.collaborative_agent.consult_gemini",
                    side_effect=["Luna, that detail is not correct.", "SKIP", "SKIP"],
                ) as gemini, patch(
                    "features.feature_agent.collaborative_agent.consult_deepseek",
                    side_effect=["Gemini's correction changes one part of my answer.", "SKIP", "SKIP"],
                ) as deepseek, patch(
                    "features.feature_agent.collaborative_agent.save_team_exchange"
                ) as save_exchange:
            responses = await self.agent.handle(self.app, message)

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
        save_exchange.assert_called_once()

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
            responses = await self.agent.handle(self.app, message)

        self.assertEqual([item.text for item in responses], ["**DeepSeek**\nHello, Master."])

    async def test_deepseek_announces_then_asks_luna_in_separate_message(self):
        platform = StubLiveRoomPlatform()
        self.app.platform = platform
        message = self._message("$agent explain this unfamiliar topic")

        def luna_reply(_request, _images=None):
            if any(content.startswith("**Luna**") for _, content in platform.sent):
                return "SKIP"
            self.assertEqual([content for _, content in platform.sent], [
                "**DeepSeek**\nI have not heard of this. Let me ask Luna about it.",
                "**DeepSeek**\nLuna, can you explain this topic to us?",
            ])
            return "Of course. Here is what it means."

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="I have not heard of this. Let me ask Luna about it. <<ASK:Luna>>",
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
            responses = await self.agent.handle(self.app, message)

        self.assertEqual(len(responses), 3)
        self.assertEqual(
            responses[0].text,
            "**DeepSeek**\nI have not heard of this. Let me ask Luna about it.",
        )
        self.assertEqual(responses[1].text, "**DeepSeek**\nLuna, can you explain this topic to us?")
        self.assertEqual(responses[2].text, "**Luna**\nOf course. Here is what it means.")
        self.assertTrue(all(response.metadata.get("sent_live") for response in responses))
        self.assertNotIn("<<ASK:", "\n".join(response.text or "" for response in responses))
        self.assertIn("直接点名你", luna.call_args.args[0])

    async def test_deepseek_announces_then_asks_claude_in_separate_message(self):
        message = self._message("$agent compare these interpretations")

        with patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"}), \
                patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=[]), \
                patch(
                    "features.feature_agent.unified_chat.query_chat",
                    return_value="There are competing assumptions here. Let me ask Claude. <<ASK:Claude>>",
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
            responses = await self.agent.handle(self.app, message)

        self.assertEqual([response.text for response in responses], [
            "**DeepSeek**\nThere are competing assumptions here. Let me ask Claude.",
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
            response = await self.agent.handle(self.app, message)

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
            response = await self.agent.handle(self.app, self._message("$agent show status"))

        self.assertIn("$system", response[0].text)

    async def test_web_search_returns_synthesized_answer_without_tool_wrapper(self):
        search = StubSearchFeature()
        agent = AgentFeature(StubTimeFeature(), StubFeature(), search, StubFeature())
        message = self._message("$agent what changed in DeepSeek today?")
        plan = [{"tool": "web_search", "arguments": {"query": "DeepSeek latest changes"}}]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            response = await agent.handle(self.app, message)

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
            response = await agent.handle(self.app, message)

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
            response = await agent.handle(self.app, message)

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
            responses = await agent.handle(self.app, message)

        self.assertEqual(query.call_count, 1)
        self.assertEqual(query.call_args.args[-1], "high")
        self.assertEqual(query.call_args.args[6], [])
        self.assertEqual(responses[0].text, "**DeepSeek**\nWhale final [1].")
        self.assertEqual(responses[1].text, "**Luna**\n来源与结论一致，完整清单紧随其后。")
        self.assertIn("Google Search: Source", responses[2].text)
        self.assertTrue(responses[2].metadata["suppress_embeds"])
        self.assertIn("最终来源审计回合", luna.call_args_list[1].args[0])
        self.assertIn("https://example.com", luna.call_args_list[1].args[0])

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

    async def test_clip_tool_runs_preparation_immediately_and_preserves_attachment(self):
        attachment = BotAttachment(path="/tmp/clip.mp4")
        clips = StubFeature([BotResponse(text="clip ready", attachment=attachment)])
        agent = AgentFeature(StubTimeFeature(), StubFeature(), StubFeature(), StubFeature(), clip_feature=clips)
        plan = [{
            "tool": "clip_video",
            "arguments": {"url": "https://youtu.be/example", "start": "1:05", "end": "1:15"},
        }]

        with patch("features.feature_agent.unified_chat.plan_agent_actions", return_value=plan):
            result = await agent.handle(self.app, self._message("$agent clip this video for me"))

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
            result = await agent.handle(self.app, self._message("$agent process this video"))

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

    def _message(self, content):
        return IncomingMessage(**{**self.message.__dict__, "content": content})


if __name__ == "__main__":
    unittest.main()
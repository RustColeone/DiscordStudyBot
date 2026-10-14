import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.bot_types import BotResponse, IncomingMessage
from features.feature_agent import AgentFeature
from services.agent_task import AgentTask
from services.command_registry import CommandRegistry, CommandTool
from services.shared_room import SharedRoom, RoomState, RoomJob, RoomTurn, parse_turn
from services.tool_outcome import ToolOutcome


def action(value="logs", tool="read"):
    return {"tool": tool, "arguments": {"value": value}}


def proposal(key, name, after=None):
    return {"id": key, "character": name, "instruction": "Inspect " + key, "after": after or []}


def encoded(message="I'm here!", status="complete", actions=None, jobs=None):
    return json.dumps({"status": status, "message": message, "actions": actions or [], "jobs": jobs or []})


class SharedRoomTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        for name, risk in (("read", "read_only"), ("write", "write"), ("prepare", "prepare")):
            self.registry.register(CommandTool(name, name, {"value": {"type": "string"}}, ["value"], risk, AsyncMock()))
        self.generate = AsyncMock(return_value=RoomTurn("complete", "I'm here."))
        self.execute = AsyncMock(return_value=ToolOutcome("success", [], "Verified result"))
        self.publish, self.progress, self.approval = AsyncMock(), AsyncMock(), AsyncMock()
        def validate(actions):
            for item in actions:
                self.registry.validate_arguments(self.registry.get(item["tool"]), item["arguments"])
        self.loop = SharedRoom(self.registry, self.generate, self.execute, self.publish,
                               self.progress, self.approval, validate)
        self.state = RoomState(AgentTask("Help the group"))

    def job(self, key="root", character="Luna", after=None):
        self.state.jobs[key] = RoomJob(key, character, key, after or [])

    async def test_fast_character_speaks_while_slow_character_is_pending(self):
        release, fast_spoken = asyncio.Event(), asyncio.Event()
        self.job("slow", "DeepSeek")
        self.job("fast", "Luna")
        async def generate(job, context, correction):
            if job.id == "slow":
                await release.wait()
            return RoomTurn("complete", job.id)
        async def publish(name, message):
            if message == "fast":
                fast_spoken.set()
        self.generate.side_effect, self.publish.side_effect = generate, publish
        running = asyncio.create_task(self.loop.run(self.state))
        try:
            await asyncio.wait_for(fast_spoken.wait(), 1)
            self.assertFalse(running.done())
        finally:
            release.set()
            await running
        self.assertEqual(self.state.status, "complete")

    async def test_teammates_own_independent_steps_and_verifier_sees_both_results(self):
        self.job()
        first, second = asyncio.Event(), asyncio.Event()
        async def generate(job, context, correction):
            if job.id == "root":
                return RoomTurn("continue", "Let's inspect both signals.", jobs=[
                    proposal("metrics", "Gemini"), proposal("logs", "Claude"),
                    proposal("verify", "DeepSeek", ["metrics", "logs"])])
            if job.id == "verify":
                self.assertEqual({r["summary"] for r in context["shared_results"]}, {"metrics result", "logs result"})
            if not job.observations and job.id != "verify":
                return RoomTurn("continue", "I'll inspect " + job.id, [action(job.id)])
            return RoomTurn("complete", "Checked " + job.id)
        async def execute(name, item):
            if item["arguments"]["value"] == "metrics":
                first.set()
                await asyncio.wait_for(second.wait(), 1)
            else:
                second.set()
                await asyncio.wait_for(first.wait(), 1)
            return ToolOutcome("success", [], item["arguments"]["value"] + " result")
        self.generate.side_effect, self.execute.side_effect = generate, execute
        await self.loop.run(self.state)
        self.assertEqual(self.state.status, "complete")
        self.assertEqual({call.args[0] for call in self.execute.await_args_list}, {"Gemini", "Claude"})
        self.assertEqual(self.state.jobs["verify"].status, "complete")

    async def test_identical_concurrent_reads_are_executed_once(self):
        self.job("one", "Luna")
        self.job("two", "Gemini")
        async def generate(job, context, correction):
            if not job.observations:
                return RoomTurn("continue", "Check logs.", [action()])
            return RoomTurn("complete", "Checked.")
        async def execute(*args):
            await asyncio.sleep(.01)
            return ToolOutcome("success", [], "Shared logs")
        self.generate.side_effect, self.execute.side_effect = generate, execute
        await self.loop.run(self.state)
        self.assertEqual(self.execute.await_count, 1)
        self.assertTrue(all(j.observations[0]["summary"] == "Shared logs" for j in self.state.jobs.values()))

    async def test_approval_does_not_block_independent_job_and_exact_write_resumes_once(self):
        self.job("write", "Luna")
        self.job("read", "Gemini")
        async def generate(job, context, correction):
            if job.id == "write" and not job.observations:
                return RoomTurn("continue", "I'll update it.", [action("config", "write")])
            return RoomTurn("complete", "Verified.")
        self.generate.side_effect = generate
        await self.loop.run(self.state)
        self.assertEqual(self.state.jobs["read"].status, "complete")
        self.execute.assert_not_awaited()
        self.assertEqual(self.state.status, "awaiting_confirmation")
        await self.loop.run(self.state, "confirm", confirm=True)
        self.execute.assert_awaited_once_with("Luna", action("config", "write"))
        self.assertEqual(self.state.status, "complete")

    async def test_duplicate_prepare_is_not_executed_or_delivered_twice(self):
        self.job("one", "Luna")
        self.job("two", "Claude")
        async def generate(job, context, correction):
            return RoomTurn("complete", "Delivered.") if job.observations else RoomTurn(
                "continue", "I'll prepare it.", [action("video", "prepare")])
        self.generate.side_effect = generate
        await self.loop.run(self.state)
        self.assertEqual(self.execute.await_count, 1)
        self.assertEqual(self.state.status, "complete")

    async def test_changed_user_answer_invalidates_unexecuted_write_before_reapproval(self):
        self.job()
        self.generate.return_value = RoomTurn("continue", "Update it.", [action("old", "write")])
        await self.loop.run(self.state)
        self.generate.return_value = RoomTurn("continue", "Use the new value.", [action("new", "write")])
        await self.loop.run(self.state, "Actually use the new value")
        self.execute.assert_not_awaited()
        self.assertEqual(self.state.jobs["root"].pending_actions, [action("new", "write")])
        self.generate.return_value = RoomTurn("complete", "Updated.")
        await self.loop.run(self.state, "confirm", confirm=True)
        self.execute.assert_awaited_once_with("Luna", action("new", "write"))

    async def test_public_work_note_precedes_message_and_tool_without_another_model_call(self):
        self.job()
        order = []
        async def note(character, text):
            order.append(("note", text))
        async def publish(character, text):
            order.append(("message", text))
        async def execute(character, item):
            order.append(("tool", item["tool"]))
            return ToolOutcome("success", [], "Verified logs")
        self.loop.publish_note = note
        self.publish.side_effect, self.execute.side_effect = publish, execute
        self.generate.side_effect = [
            RoomTurn("continue", "I'll check now.", [action()], work_note="A fresh reading will verify the old report."),
            RoomTurn("complete", "Checked."),
        ]
        await self.loop.run(self.state)
        self.assertEqual([kind for kind, _ in order], ["note", "message", "tool", "message"])
        self.assertEqual(self.generate.await_count, 2)
        self.assertEqual(self.state.messages[0]["work_note"], "A fresh reading will verify the old report.")

    async def test_normalized_provider_call_still_requires_write_approval(self):
        self.job()
        self.generate.return_value = parse_turn(encoded("I'll update it.", "continue", [
            {"name": "write", "arguments": {"value": "config"}}]))
        await self.loop.run(self.state)
        self.execute.assert_not_awaited()
        self.assertEqual(self.state.status, "awaiting_confirmation")
        self.assertEqual(self.state.jobs["root"].pending_actions, [action("config", "write")])

    async def test_normalized_provider_call_still_rejects_unknown_arguments(self):
        self.job()
        self.generate.return_value = parse_turn(encoded("I'll read it.", "continue", [
            {"name": "read", "arguments": {"value": "config", "unexpected": True}}]))
        await self.loop.run(self.state)
        self.execute.assert_not_awaited()
        self.assertEqual(self.state.status, "blocked")

    async def test_protocol_failure_is_localized_and_diagnostics_do_not_leak_arguments(self):
        self.state.task.goal = "帮我下载这个视频"
        self.job()
        self.generate.side_effect = ValueError('private argument details')
        with patch("services.shared_room.record_timing") as timing:
            await self.loop.run(self.state)
        spoken = self.publish.await_args.args[1]
        self.assertIn("没能可靠地启动", spoken)
        self.assertNotIn("private argument", spoken)
        self.assertNotIn("private argument", str(timing.call_args_list))

    async def test_failed_prerequisite_blocks_downstream_but_other_jobs_finish(self):
        self.job("source", "Luna")
        self.job("deliver", "Gemini", ["source"])
        self.job("independent", "Claude")
        async def generate(job, context, correction):
            if job.id == "source":
                if not job.observations:
                    return RoomTurn("continue", "Fetch source.", [action(), action("must-not-run", "prepare")])
                return RoomTurn("blocked", "Source unavailable.")
            self.assertNotEqual(job.id, "deliver")
            return RoomTurn("complete", "Checked independently.")
        self.generate.side_effect = generate
        self.execute.return_value = ToolOutcome("failed", [], "No video formats", "source_unavailable")
        await self.loop.run(self.state)
        self.assertEqual(self.execute.await_count, 1)
        self.assertEqual(self.state.jobs["deliver"].status, "blocked")
        self.assertEqual(self.state.jobs["independent"].status, "complete")
        self.assertEqual(self.state.status, "blocked")

    async def test_cycles_rejected_before_proposals_or_actions_are_committed(self):
        self.job()
        self.generate.return_value = RoomTurn("continue", "Check.", [action()], [
            proposal("a", "Claude", ["b"]), proposal("b", "Gemini", ["a"])])
        await self.loop.run(self.state)
        self.execute.assert_not_awaited()
        self.assertEqual(list(self.state.jobs), ["root"])
        self.assertEqual(self.state.status, "blocked")

    async def test_false_completion_after_failure_is_rejected(self):
        self.job()
        self.generate.side_effect = [RoomTurn("continue", "Try.", [action()]),
                                    RoomTurn("complete", "Done."), RoomTurn("blocked", "Could not finish.")]
        self.execute.return_value = ToolOutcome("failed", [], "No source")
        await self.loop.run(self.state)
        self.assertEqual(self.state.status, "blocked")
        self.assertNotIn("Done.", [c.args[1] for c in self.publish.await_args_list])

    async def test_missing_input_resumes_same_job_with_original_goal_and_question(self):
        self.job()
        self.generate.return_value = RoomTurn("needs_input", "Which server?")
        await self.loop.run(self.state)
        self.generate.return_value = RoomTurn("complete", "Understood.")
        await self.loop.run(self.state, "home server")
        context = self.generate.await_args.args[1]
        self.assertEqual(context["user_answers"], ["home server"])
        self.assertEqual(context["pending_question"], "Which server?")
        self.assertEqual(context["goal"], "Help the group")

    async def test_cancel_cleans_up_inflight_shared_read(self):
        self.job()
        started, cleaned = asyncio.Event(), asyncio.Event()
        self.generate.return_value = RoomTurn("continue", "Read.", [action()])
        async def execute(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.execute.side_effect = execute
        running = asyncio.create_task(self.loop.run(self.state))
        await asyncio.wait_for(started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(cleaned.is_set())


class RoomFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stub = SimpleNamespace(handle=AsyncMock(return_value=[BotResponse(text="healthy")]))
        self.feature = AgentFeature(stub, stub, stub, stub)
        self.app = SimpleNamespace(config={"TIMEZONE": "UTC"})
        self.message = IncomingMessage("$agent 你们在吗？", "room", "user", "User", "User", datetime.now(timezone.utc))
        self.settings = patch("features.feature_agent.db.get_channel_settings", return_value={"mode": "agent"})
        self.settings.start()
        self.addCleanup(self.settings.stop)

    async def test_group_chat_uses_same_loop_without_router_planner_or_summary(self):
        with patch("features.feature_agent.collaborative_agent.query_agent", return_value=encoded()) as deepseek, \
                patch.object(self.feature, "_consult_room_member", new=AsyncMock(return_value=encoded())) as members, \
                patch("features.feature_agent.unified_chat.plan_agent_actions") as planner, \
                patch("features.feature_agent.collaborative_agent.query_room_route") as router, \
                patch("features.feature_agent.unified_chat.generate_process_summary") as summary, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.feature.handle(self.app, self.message)
        self.assertEqual(len(responses), 4)
        self.assertEqual(deepseek.call_count, 1)
        self.assertEqual(members.await_count, 3)
        planner.assert_not_called()
        router.assert_not_called()
        summary.assert_not_called()
        self.assertEqual(self.feature.room_sessions, {})

    async def test_a_character_can_do_tool_work_without_routing_or_handoff(self):
        self.message.content = "$agent Luna inspect my server"
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=[
                encoded("I'll inspect it.", "continue", [{"tool": "system_status", "arguments": {}}]),
                encoded("The server is healthy.")])) as member, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.feature.handle(self.app, self.message)
        self.assertEqual(member.await_count, 2)
        self.assertIn('"summary": "healthy"', member.await_args.args[1])
        self.assertEqual(member.await_args.kwargs["effort"], "medium")
        self.assertIn("I'll inspect it.", responses[0].text)
        self.assertIn("healthy", responses[-1].text)

    async def test_only_each_characters_own_diary_is_injected(self):
        self.message.content = "$agent Luna and Gemini hi"
        async def member(name, request, images, context, **options):
            self.assertIn(name + "-private", context)
            self.assertNotIn(("Gemini" if name == "Luna" else "Luna") + "-private", context + request)
            return encoded(name)
        with patch("features.feature_agent.diary_context", side_effect=lambda msg, name: name + "-private"), \
                patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=member)), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.feature.handle(self.app, self.message)

    async def test_character_work_note_is_displayed_and_saved_as_public_text(self):
        self.message.content = "$agent Luna inspect my server"
        value = json.loads(encoded("我先检查一下。", "continue", [{"tool": "system_status", "arguments": {}}]))
        value["work_note"] = "先核实当前负载，再判断是否需要处理。"
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=[
                json.dumps(value, ensure_ascii=False), encoded("状态正常。")])) as model, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as save:
            responses = await self.feature.handle(self.app, self.message)
        self.assertEqual(model.await_count, 2)
        self.assertTrue(responses[0].metadata["room_work_note"])
        self.assertIn("Luna · 工作说明", responses[0].text)
        self.assertIn(value["work_note"], save.call_args.args[1])
        self.assertIn("我先检查一下", responses[1].text)

    async def test_structured_diary_is_private_and_format_scope_is_reset(self):
        from services.room_protocol import structured_room
        self.message.content = "$agent Luna inspect my server"
        private_note = "Prefers quiet evenings for reading."
        value = json.loads(encoded("Checking.", "continue", [{"tool": "system_status", "arguments": "{}"}]))
        value["diary_updates"] = [{"key": "reading.time", "kind": "fact", "note": private_note, "importance": 3}]
        calls = 0
        async def member(name, request, images, context, **options):
            nonlocal calls
            self.assertTrue(structured_room.get())
            self.assertNotIn(private_note, request)
            calls += 1
            return json.dumps(value) if calls == 1 else encoded("Checked.")
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=member)), \
                patch("services.character_diary.db.save_diary_note") as diary, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange") as transcript:
            responses = await self.feature.handle(self.app, self.message)
        diary.assert_called_once()
        self.assertEqual(diary.call_args.args[1], "Luna")
        self.assertIn(private_note, diary.call_args.args)
        self.assertNotIn(private_note, str(responses) + transcript.call_args.args[1])
        self.assertFalse(structured_room.get())

    async def test_invalid_native_response_cannot_leak_json_or_claim_success(self):
        self.message.content = "$agent Luna hi"
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(return_value="not JSON")), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            responses = await self.feature.handle(self.app, self.message)
        self.assertEqual(len(responses), 1)
        self.assertIn("couldn't start this step reliably", responses[0].text)

    async def test_approval_resumes_same_room_and_preserves_original_time(self):
        self.message.content = "$agent Luna remind me in two hours"
        original = self.message.created_at
        calls = []
        def reminder(app, message, **arguments):
            calls.append((message.created_at, arguments))
            return [BotResponse(text="scheduled")]
        self.feature.time_feature.create_structured_reminder = reminder
        reminder_action = {"tool": "create_reminder", "arguments": {
            "message": "study", "schedule_type": "relative", "delay_value": 2, "delay_unit": "hours"}}
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=[
                encoded("I'll set it.", "continue", [reminder_action]), encoded("It's scheduled.")])), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.feature.handle(self.app, self.message)
            self.assertEqual(calls, [])
            self.message.content = "$agent confirm"
            self.message.created_at = datetime.now(timezone.utc)
            await self.feature.handle(self.app, self.message)
        self.assertEqual(calls[0][0], original)
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.feature.room_sessions)

    async def test_new_image_is_fetched_once_before_all_characters_see_it(self):
        self.message.metadata["attachments"] = [{"url": "https://example.test/photo.png", "filename": "photo.png"}]
        images = [{"path": "/tmp/photo.png", "url": "https://example.test/photo.png", "content_type": "image/png"}]
        async def execute(name, app, message, arguments):
            return [BotResponse(text="Photo fetched", metadata={"tool_images": images})]
        async def member(name, request, attached, context, **options):
            self.assertEqual(attached, images)
            return encoded("Looks good.")
        with patch.object(self.feature.registry, "execute", new=AsyncMock(side_effect=execute)) as read, \
                patch("features.feature_agent.db.save_conversation_attachments"), \
                patch("features.feature_agent.db.list_conversation_attachments", return_value=[{
                    "id": "photo", "url": "https://example.test/photo.png", "filename": "photo.png", "content_type": "image/png"}]), \
                patch("features.feature_agent.collaborative_agent.query_agent", return_value=encoded()) as deepseek, \
                patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=member)), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.feature.handle(self.app, self.message)
        self.assertEqual(read.await_count, 1)
        self.assertEqual(deepseek.call_args.args[6], images)

    async def test_same_channel_messages_cannot_race_shared_state(self):
        self.message.content = "$agent Luna hello"
        began, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def member(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                began.set()
                await release.wait()
            return encoded()
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(side_effect=member)), \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            first = asyncio.create_task(self.feature.handle(self.app, self.message))
            await asyncio.wait_for(began.wait(), 1)
            second = asyncio.create_task(self.feature.handle(self.app, self.message))
            await asyncio.sleep(.01)
            self.assertEqual(count, 1)
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(count, 2)
        self.assertFalse(self.feature.room_locks)

    async def test_explicit_trace_preserves_observable_progress_for_chat(self):
        self.message.content = "$agent --trace Luna hi"
        with patch.object(self.feature, "_consult_room_member", new=AsyncMock(return_value=encoded())), \
                patch.object(self.feature, "_finish_progress", new=AsyncMock()) as finish, \
                patch("features.feature_agent.collaborative_agent.save_team_exchange"):
            await self.feature.handle(self.app, self.message)
        self.assertTrue(finish.await_args.kwargs["preserve"])


class RoomParserTests(unittest.TestCase):
    def test_chat_and_work_use_same_turn_contract(self):
        self.assertEqual(parse_turn(encoded()).status, "complete")
        self.assertEqual(parse_turn(encoded("Check.", "continue", [action()])).actions, [action()])
        with self.assertRaises(ValueError):
            parse_turn(encoded("Check.", "continue"))

    def test_equivalent_tool_envelopes_normalize_without_changing_arguments(self):
        variants = [
            {"name": "download_video", "arguments": {"url": "https://example.test/video", "fps": 30}},
            {"type": "function", "function": {"name": "download_video", "arguments":
                '{"url":"https://example.test/video","fps":30}'}},
        ]
        for item in variants:
            with self.subTest(item=item):
                parsed = parse_turn(encoded("下载整段。", "continue", [item]))
                self.assertEqual(parsed.actions, [{"tool": "download_video", "arguments": {
                    "url": "https://example.test/video", "fps": 30}}])

    def test_ambiguous_envelopes_and_extra_approval_fields_are_rejected(self):
        for item in (
            {"tool": "write", "name": "read", "arguments": {}},
            {"tool": "write", "arguments": {}, "approved": True},
            {"name": "write", "parameters": {}},
        ):
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_turn(encoded("Check.", "continue", [item]))

    def test_work_note_is_optional_and_bounded_public_text(self):
        value = json.loads(encoded())
        value["work_note"] = "先核实数据，再下结论。"
        self.assertEqual(parse_turn(json.dumps(value)).work_note, value["work_note"])
        value["work_note"] = "x" * 481
        with self.assertRaises(ValueError):
            parse_turn(json.dumps(value))

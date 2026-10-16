from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.bot_types import BotResponse
from services.agent_loop import AgentLoop, AgentRun, AgentStep, parse_step, signature
from services.agent_task import AgentTask
from services.command_registry import CommandRegistry, CommandTool
from services.tool_outcome import ToolOutcome, normalize_outcome


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        for name, risk in [("inspect", "read_only"), ("recover", "prepare"), ("change", "write")]:
            self.registry.register(CommandTool(name, name, {"target": {"type": "string"}}, ["target"], risk, AsyncMock()))
        self.state = AgentRun(AgentTask("finish the requested operation"))
        self.timeline = []
        async def publish(message):
            self.timeline.append(("say", message))
        self.publish = publish
        self.progress = AsyncMock()
        self.approval = AsyncMock()

    def action(self, tool="inspect", target="server"):
        return {"tool": tool, "arguments": {"target": target}}

    def validate(self, actions):
        for action in actions:
            if set(action) != {"tool", "arguments"}:
                raise ValueError("invalid action")
            self.registry.validate_arguments(self.registry.get(action["tool"]), action["arguments"])

    def loop(self, generate, execute, **kwargs):
        return AgentLoop(self.registry, generate, execute, self.publish, self.progress,
                         self.approval, self.validate, **kwargs)

    async def test_speaks_before_tools_and_continues_with_one_owner_until_verified(self):
        decisions = [AgentStep("continue", "I'll inspect the service.", [self.action()]),
                     AgentStep("continue", "The config needs repair; I'll prepare it.", [self.action("recover")]),
                     AgentStep("continue", "I'll verify the result.", [self.action()]),
                     AgentStep("complete", "The requested operation is verified.")]
        generate = AsyncMock(side_effect=decisions)
        async def execute(action):
            self.timeline.append(("tool", action["tool"]))
            return "verified result"
        await self.loop(generate, execute).run(self.state)
        self.assertEqual([kind for kind, _ in self.timeline], ["say", "tool", "say", "tool", "say", "tool", "say"])
        self.assertEqual(generate.await_count, 4)
        self.assertEqual(self.state.status, "complete")
        self.assertEqual([o["status"] for o in self.state.observations], ["success"] * 3)

    async def test_failure_stops_dependent_batch_and_rejects_false_completion(self):
        generate = AsyncMock(side_effect=[
            AgentStep("continue", "I'll inspect first.", [self.action(), self.action("recover")]),
            AgentStep("complete", "Everything succeeded."), AgentStep("blocked", "The source is unavailable."),
        ])
        execute = AsyncMock(return_value=[BotResponse(text="❌ No formats", metadata={"error_code": "source_unavailable"})])
        await self.loop(generate, execute).run(self.state)
        execute.assert_awaited_once()
        self.assertEqual(self.state.status, "blocked")
        self.assertNotIn(("say", "Everything succeeded."), self.timeline)
        self.assertIn("latest tool result failed", generate.await_args.args[2])
        self.assertEqual(self.state.task.events[-1].event_type, "agent.step")

    async def test_different_supported_recovery_can_finish_after_failure(self):
        generate = AsyncMock(side_effect=[
            AgentStep("continue", "Checking.", [self.action()]),
            AgentStep("continue", "Trying the supported alternative.", [self.action("recover")]),
            AgentStep("complete", "Verified."),
        ])
        execute = AsyncMock(side_effect=[ToolOutcome("failed", [], "primary unavailable"), "alternative verified"])
        await self.loop(generate, execute).run(self.state)
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(self.state.status, "complete")

    async def test_approval_resumes_exact_action_then_verifies_without_replanning(self):
        generate = AsyncMock(side_effect=[AgentStep("continue", "I'll apply this change.", [self.action("change")]),
                                         AgentStep("complete", "Applied and verified.")])
        execute = AsyncMock(return_value="write verified")
        loop = self.loop(generate, execute)
        await loop.run(self.state)
        self.assertEqual(self.state.status, "awaiting_confirmation")
        execute.assert_not_awaited()
        await loop.run(self.state, "confirm", confirm=True)
        execute.assert_awaited_once_with(self.action("change"))
        self.approval.assert_awaited_once()
        self.assertEqual(self.state.status, "complete")
        self.assertEqual(generate.await_count, 2)

    async def test_successful_write_cannot_run_twice_after_approval(self):
        generate = AsyncMock(side_effect=[AgentStep("continue", "Apply.", [self.action("change")]),
                                         AgentStep("continue", "Apply again.", [self.action("change")]),
                                         AgentStep("continue", "Apply once more.", [self.action("change")])])
        execute = AsyncMock(return_value="write verified")
        loop = self.loop(generate, execute)
        await loop.run(self.state)
        await loop.run(self.state, confirm=True)
        execute.assert_awaited_once()
        self.assertEqual(self.state.status, "complete")
        self.assertEqual([o["error_code"] for o in self.state.observations], [None, "already_completed"])
        self.assertIn("already succeeded", self.state.observations[-1]["summary"])
        # No fixed internal sentence reaches the conversation; only the model's own turns are spoken.
        self.assertEqual([text for kind, text in self.timeline if kind == "say"], ["Apply.", "Apply again.", "Apply once more."])

    async def test_repeated_prepare_is_reported_to_model_which_can_finish_normally(self):
        generate = AsyncMock(side_effect=[AgentStep("continue", "Drawing.", [self.action("recover", "image")]),
                                         AgentStep("continue", "Drawing again.", [self.action("recover", "image")]),
                                         AgentStep("complete", "The image is already in the chat.")])
        async def execute(action):
            # The WeChat adapter marks a successful prepare (such as a delivered image) as completed.
            self.state.writes_completed.add(signature(action))
            return "image delivered"
        await self.loop(generate, execute).run(self.state)
        self.assertEqual(self.state.status, "complete")
        self.assertEqual(self.timeline[-1], ("say", "The image is already in the chat."))
        self.assertEqual([o["error_code"] for o in self.state.observations], [None, "already_completed"])

    async def test_nonretryable_failure_cannot_repeat_unchanged(self):
        generate = AsyncMock(return_value=AgentStep("continue", "Trying.", [self.action()]))
        execute = AsyncMock(return_value=ToolOutcome("failed", [], "source failed", "source_unavailable", False))
        await self.loop(generate, execute).run(self.state)
        execute.assert_awaited_once()
        self.assertEqual(self.state.status, "blocked")

    async def test_transient_read_failure_allows_one_retry(self):
        generate = AsyncMock(side_effect=[AgentStep("continue", "Checking.", [self.action()]),
                                         AgentStep("continue", "Retrying the connection.", [self.action()]),
                                         AgentStep("complete", "Verified.")])
        execute = AsyncMock(side_effect=[TimeoutError("temporary"), "verified"])
        await self.loop(generate, execute).run(self.state)
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(self.state.status, "complete")

    async def test_invalid_arguments_are_repaired_before_speaking_or_execution(self):
        generate = AsyncMock(side_effect=[AgentStep("continue", "Bad plan.", [{"tool": "inspect", "arguments": {}}]),
                                         AgentStep("continue", "Checking.", [self.action()]),
                                         AgentStep("complete", "Verified.")])
        execute = AsyncMock(return_value="verified")
        await self.loop(generate, execute).run(self.state)
        execute.assert_awaited_once()
        self.assertNotIn(("say", "Bad plan."), self.timeline)

    async def test_needs_input_resumes_the_same_goal_and_observations(self):
        generate = AsyncMock(side_effect=[AgentStep("needs_input", "Which service?"),
                                         AgentStep("continue", "Checking that service.", [self.action()]),
                                         AgentStep("complete", "Verified.")])
        loop = self.loop(generate, AsyncMock(return_value="verified"))
        await loop.run(self.state)
        self.assertEqual(self.state.status, "needs_input")
        await loop.run(self.state, "the music service")
        self.assertEqual(generate.await_args.args[1], "the music service")
        self.assertEqual(self.state.task.goal, "finish the requested operation")
        self.assertEqual(self.state.user_answers, ["the music service"])
        self.assertEqual(self.state.status, "complete")

    async def test_limits_report_unfinished_task(self):
        generate = AsyncMock(return_value=AgentStep("continue", "Checking.", [self.action()]))
        execute = AsyncMock(return_value="verified")
        await self.loop(generate, execute, max_steps=2).run(self.state)
        self.assertEqual(self.state.status, "blocked")
        self.assertIn("unfinished", self.timeline[-1][1])
        # The closing call proposed more actions, so the neutral fallback is used; the reason stays internal.
        self.assertNotIn("step limit", self.timeline[-1][1])
        self.assertTrue(any("step limit" in call.args[0] for call in self.progress.await_args_list))

    async def test_stop_message_is_written_by_the_model_without_internal_reason(self):
        state = AgentRun(AgentTask("帮我下载这个视频"))
        generate = AsyncMock(side_effect=[
            AgentStep("continue", "好的，我去下载。", [self.action()]),
            AgentStep("continue", "再试一次。", [self.action()]),
            AgentStep("blocked", "视频源这次拿不到，先到这里吧。")])
        execute = AsyncMock(return_value=ToolOutcome("failed", [], "No video formats found", "source_unavailable", False))
        await self.loop(generate, execute).run(state)
        self.assertEqual(state.status, "blocked")
        spoken = [text for kind, text in self.timeline if kind == "say"]
        self.assertEqual(spoken[-1], "视频源这次拿不到，先到这里吧。")
        self.assertFalse(any("Repeating" in text or "No video formats" in text for text in spoken))
        self.assertIn("Internal reason", generate.await_args.args[2])
        self.assertIn("No video formats", generate.await_args.args[2])

    async def test_model_failure_uses_localized_fallback_without_another_model_call(self):
        state = AgentRun(AgentTask("画一张龙娘"))
        generate = AsyncMock(side_effect=RuntimeError("provider down"))
        await self.loop(generate, AsyncMock()).run(state)
        generate.assert_awaited_once()
        self.assertEqual(self.timeline, [("say", "这次没能全部完成，已经做好的部分都保留着。需要的话可以再说一次。")])
        self.assertTrue(any("RuntimeError" in call.args[0] for call in self.progress.await_args_list))

    async def test_tool_needing_input_cannot_run_more_actions_before_an_answer(self):
        generate = AsyncMock(side_effect=[
            AgentStep('continue', 'I’ll download it.', [self.action('recover')]),
            AgentStep('continue', 'I’ll try the download again.', [self.action('recover')]),
            AgentStep('needs_input', 'Would you prefer a larger file or a shorter clip?'),
        ])
        execute = AsyncMock(return_value=ToolOutcome('needs_input', [], 'A user size choice is required'))
        loop = self.loop(generate, execute)
        await loop.run(self.state)
        execute.assert_awaited_once()
        self.assertEqual(self.state.status, 'needs_input')
        self.assertTrue(self.state.input_required)
        self.assertNotIn(('say', 'I’ll try the download again.'), self.timeline)
        self.assertIn('new user answer', generate.await_args.args[2])
        self.assertEqual(self.state.context()['visible_turns'], [
            'I’ll download it.', 'Would you prefer a larger file or a shorter clip?'])

        # Polling or an approval event is not an answer to that question.
        await loop.run(self.state)
        await loop.run(self.state, confirm=True)
        self.assertEqual(generate.await_count, 3)

        generate.side_effect = [AgentStep('continue', 'Using the larger budget.', [self.action('recover')]),
                                AgentStep('complete', 'Verified.')]
        execute.return_value = ToolOutcome('success', [], 'Delivered')
        await loop.run(self.state, 'Use a larger file')
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(self.state.status, 'complete')
        self.assertFalse(self.state.input_required)

    async def test_input_required_stops_dependent_actions_and_false_completion(self):
        generate = AsyncMock(side_effect=[
            AgentStep('continue', 'Inspecting.', [self.action(), self.action('recover')]),
            AgentStep('complete', 'Done.'),
            AgentStep('needs_input', 'Which account should I use?'),
        ])
        execute = AsyncMock(return_value=ToolOutcome('needs_input', [], 'An account choice is needed'))
        await self.loop(generate, execute).run(self.state)
        execute.assert_awaited_once_with(self.action())
        self.assertNotIn(('say', 'Done.'), self.timeline)
        self.assertEqual(self.state.status, 'needs_input')

    async def test_registered_input_tool_is_one_question_and_a_pause_not_an_action(self):
        handler=AsyncMock()
        self.registry.register(CommandTool('choose_account', 'Ask for the account',
            {'prompt':{'type':'string'}}, ['prompt'], 'read_only', handler, input_prompt_argument='prompt'))
        generate=AsyncMock(side_effect=[AgentStep('continue', 'Let me ask you something.', [
            {'tool':'choose_account','arguments':{'prompt':'Which account should I use?'}}])])
        execute=AsyncMock()
        loop=self.loop(generate,execute)
        await loop.run(self.state)
        execute.assert_not_awaited()
        handler.assert_not_awaited()
        self.assertEqual(self.timeline,[('say','Which account should I use?')])
        self.assertEqual(self.state.status,'needs_input')
        self.assertEqual(self.state.actions_taken,0)
        self.assertEqual(self.state.context()['pending_question'],'Which account should I use?')
        await loop.run(self.state)
        self.assertEqual(generate.await_count,1)

    def test_parser_rejects_invalid_control_states(self):
        for text in ['{"status":"continue","message":"Working","actions":[]}',
                     '{"status":"complete","message":"Done","actions":[{}]}',
                     '{"status":"complete","message":"","actions":[]}']:
            with self.assertRaises(ValueError):
                parse_step(text)

    def test_failure_metadata_and_legacy_error_text_are_not_success(self):
        for response in [BotResponse(text="❌ source failed"),
                         BotResponse(text="No formats found", metadata={"tool_status": "failed", "error_code": "source_unavailable"})]:
            self.assertEqual(normalize_outcome([response]).status, "failed")

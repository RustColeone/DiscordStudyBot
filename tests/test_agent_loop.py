from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from app.bot_types import BotResponse
from services.agent_loop import AgentLoop, AgentRun, AgentStep, parse_step
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
                                         AgentStep("continue", "Apply again.", [self.action("change")])])
        execute = AsyncMock(return_value="write verified")
        loop = self.loop(generate, execute)
        await loop.run(self.state)
        await loop.run(self.state, confirm=True)
        execute.assert_awaited_once()
        self.assertEqual(self.state.status, "blocked")

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

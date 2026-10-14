import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from adapters.discord_adapter import DiscordAdapter
from app.bot_app import BotApplication
from app.bot_types import BotResponse, IncomingMessage
from features.feature_agent import AgentFeature
from services.agent_task import AgentTask
from services import request_timing


class RequestTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_wait_and_executor_provider_use_same_request(self):
        message = IncomingMessage("$chat -s hello", "c", "u", "user", "user", None)
        app = BotApplication.__new__(BotApplication)
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=20, completion_tokens=8))
        provider = request_timing.trace_provider("Luna")(lambda **kwargs: response)

        async def handler(incoming):
            await AgentFeature._run_in_executor(None, lambda: provider(
                model="model", messages=[{"content": "private test prompt"}], reasoning_effort="low"))
            return [BotResponse(text="Hello")]

        app._handle_message = handler
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        channel = SimpleNamespace(send=AsyncMock(return_value=object()))
        timing = request_timing.RequestTiming()
        message.metadata["request_timing"] = timing
        with patch.object(request_timing, "_write") as write:
            timing.record("queued", active=True, ahead=1)
            answers = await app.handle_message(message)
            await adapter._send_responses(channel, answers, timing)
        events = [call.args[0] for call in write.call_args_list]
        self.assertEqual([e["stage"] for e in events],
                         ["queued", "handler_started", "provider_attempt", "handler_finished", "first_reply"])
        self.assertEqual({e["request"] for e in events}, {timing.request_id})
        self.assertEqual(events[2]["prompt_tokens"], 20)
        self.assertNotIn("private test prompt", str(events))
        self.assertIsNone(request_timing.current_timing.get())

    async def test_failed_send_does_not_count_as_first_reply(self):
        timing = request_timing.RequestTiming()
        app = SimpleNamespace(platform=SimpleNamespace(
            supports_live_responses=True, send_channel_response=AsyncMock(side_effect=RuntimeError)))
        token = request_timing.current_timing.set(timing)
        self.addCleanup(request_timing.current_timing.reset, token)
        with patch.object(request_timing, "_write") as write:
            await AgentFeature._emit_room_turn(app, SimpleNamespace(channel_id="c"),
                                              BotResponse(text="**DeepSeek**\nHello"), [])
        self.assertFalse(timing.first_delivered)
        write.assert_not_called()

    async def test_live_delivery_then_final_delivery_records_first_reply_once(self):
        timing = request_timing.RequestTiming()
        token = request_timing.current_timing.set(timing)
        self.addCleanup(request_timing.current_timing.reset, token)
        app = SimpleNamespace(platform=SimpleNamespace(
            supports_live_responses=True, send_channel_response=AsyncMock()))
        answers = []
        adapter = DiscordAdapter.__new__(DiscordAdapter)
        channel = SimpleNamespace(send=AsyncMock(return_value=object()))
        with patch.object(request_timing, "_write") as write:
            await AgentFeature._emit_room_turn(app, SimpleNamespace(channel_id="c"),
                                              BotResponse(text="**DeepSeek**\nHello"), answers)
            await adapter._send_responses(channel, answers + [BotResponse(text="Sources")], timing)
        self.assertEqual([c.args[0]["stage"] for c in write.call_args_list], ["first_reply"])
        channel.send.assert_awaited_once()

    def test_each_provider_attempt_records_errors_without_error_contents(self):
        timing = request_timing.RequestTiming()
        token = request_timing.current_timing.set(timing)
        self.addCleanup(request_timing.current_timing.reset, token)
        def fail(**kwargs):
            raise TimeoutError("private provider message")
        call = request_timing.trace_provider("DeepSeek")(fail)
        with patch.object(request_timing, "_write") as write:
            for _ in range(2):
                with self.assertRaises(TimeoutError):
                    call(model="model", messages=[], reasoning_effort="low")
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args.args[0]["error"], "TimeoutError")
        self.assertNotIn("private provider message", str(write.call_args_list))

    def test_adaptive_haiku_effort_and_thinking_usage_are_measured(self):
        timing = request_timing.RequestTiming()
        token = request_timing.current_timing.set(timing)
        self.addCleanup(request_timing.current_timing.reset, token)
        response = SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(
            input_tokens=40, output_tokens=60, output_tokens_details=SimpleNamespace(thinking_tokens=15)))
        call = request_timing.trace_provider("Claude")(lambda **kwargs: response)
        with patch.object(request_timing, "_write") as write:
            call(model="claude-haiku-5-5", output_config={"effort": "low"}, messages=[])
        event = write.call_args.args[0]
        self.assertEqual(event["effort"], "low")
        self.assertEqual(event["reasoning_tokens"], 15)

    def test_casual_prompt_preserves_tool_arguments_and_verified_outcomes(self):
        task = AgentTask("casual question", conversation_only=True, capabilities=[{
            "name": "read_attachment", "description": "redundant prose " * 100,
            "parameters": {"attachment": {"type": "string"}}, "required": ["attachment"], "risk": "read_only",
        }])
        task.record("task.created", "user", request=task.goal)
        task.record("tool.succeeded", "DeepSeek", tool="read_attachment", result="verified food photo")
        concise = AgentFeature._agent_tool_instruction(task)
        task.response_mode = "deliverable"
        detailed = AgentFeature._agent_tool_instruction(task)
        self.assertIn('"required": ["attachment"]', concise)
        self.assertIn("verified food photo", concise)
        self.assertNotIn("redundant prose", concise)
        self.assertLess(len(concise), len(detailed))
        self.assertIn("redundant prose", detailed)

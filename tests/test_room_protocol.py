import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import collaborative_agent as provider, openrouter_query
from services.room_prompt import ROOM_INSTRUCTION
from services.room_protocol import ROOM_OUTPUT_SCHEMA, room_output_mode, structured_room
from services.shared_room import parse_turn


class RoomProtocolTests(unittest.TestCase):
    def test_prompt_examples_decode_to_valid_actions(self):
        for line in ROOM_INSTRUCTION.splitlines():
            if line.startswith('{"tool":'):
                action = json.loads(line.rstrip('.'))
                self.assertIsInstance(json.loads(action['arguments']), dict)
            elif line.startswith('{"status":'):
                turn = parse_turn(line.rstrip('.'))
                self.assertEqual(turn.actions[0]['arguments'], {})

    def test_native_formats_are_enabled_only_for_room_requests(self):
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text='{}', candidates=[])
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type='text', text='{}')], stop_reason='end_turn')
        with patch.object(provider, 'openai_client', client), patch.object(provider, 'deepseek_client', client), \
                patch.object(provider, 'gemini_client', client), patch.object(provider, 'claude_client', client), \
                patch.object(provider, 'LUNA_BACKEND', 'direct'), patch.object(provider, 'DEEPSEEK_BACKEND', 'direct'), \
                patch.object(provider, '_gemini_request_slot'), patch.object(openrouter_query, 'client', client):
            with room_output_mode():
                provider._query_luna([], 'low', 2000)
                fmt = client.chat.completions.create.call_args.kwargs['response_format']
                self.assertEqual(fmt['json_schema']['schema'], ROOM_OUTPUT_SCHEMA)
                self.assertTrue(fmt['json_schema']['strict'])
                provider._query_whale([], 'low', 2000)
                self.assertEqual(client.chat.completions.create.call_args.kwargs['response_format'], {'type': 'json_object'})
                openrouter_query.create_completion([], 'test-model')
                self.assertEqual(client.chat.completions.create.call_args.kwargs['response_format'], {'type': 'json_object'})
                provider._complete_gemini_visible('test', 'test', 'standard', effort='low')
                config = client.models.generate_content.call_args.kwargs['config']
                self.assertEqual(config.response_json_schema, ROOM_OUTPUT_SCHEMA)
                self.assertEqual(config.response_mime_type, 'application/json')
                provider._complete_claude_visible([], 'test', 'standard', effort='low')
                self.assertEqual(client.messages.create.call_args.kwargs['output_config']['format']['schema'], ROOM_OUTPUT_SCHEMA)
            provider._query_luna([], 'low', 2000)
            self.assertNotIn('response_format', client.chat.completions.create.call_args.kwargs)

    def test_truncated_json_never_uses_prose_continuation(self):
        query = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"status":'), finish_reason='length')]))
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(
            text='{"status":', candidates=[SimpleNamespace(finish_reason='max_tokens')])
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type='text', text='{"status":')], stop_reason='max_tokens')
        with room_output_mode(), patch.object(provider, 'gemini_client', client), \
                patch.object(provider, 'claude_client', client), patch.object(provider, '_gemini_request_slot'):
            for call in (
                lambda: provider._complete_openai_visible(query, [], 'low', 'standard', 'test'),
                lambda: provider._complete_gemini_visible('test', 'test', 'standard', effort='low'),
                lambda: provider._complete_claude_visible([], 'test', 'standard', effort='low'),
            ):
                with self.assertRaisesRegex(ValueError, 'truncated'):
                    call()
        query.assert_called_once()
        client.models.generate_content.assert_called_once()
        client.messages.create.assert_called_once()


class RoomContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_format_propagates_to_worker_without_affecting_concurrent_chat(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def formatted():
            with room_output_mode():
                entered.set()
                await release.wait()
                self.assertTrue(await asyncio.to_thread(structured_room.get))
            self.assertFalse(structured_room.get())
        task = asyncio.create_task(formatted())
        await entered.wait()
        self.assertFalse(structured_room.get())
        release.set()
        await task
        with self.assertRaises(ValueError):
            with room_output_mode():
                raise ValueError('test')
        self.assertFalse(structured_room.get())

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from contracts import ModelError, ModelRequest
from core.providers.adapters import AdapterResult, Anthropic, Gemini, OpenAICompatible
from core.providers.registry import Providers
from tests.core_support import FakeAdapter

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"],
          "additionalProperties": False}
REQUEST = ModelRequest([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                       output_schema=SCHEMA, schema_name="reply")


class ProvidersTests(unittest.IsolatedAsyncioTestCase):
    def providers(self, first, second=None):
        adapters = {"a": first, **({"b": second} if second else {})}
        profile = [{"provider": "a", "model": "m1"}] + ([{"provider": "b", "model": "m2"}] if second else [])
        records = []
        return Providers({}, {"default": profile}, adapters=adapters,
                         record=lambda event, **fields: records.append((event, fields))), records

    async def test_fallback_on_error_and_schema_parsing_with_fences(self):
        providers, records = self.providers(FakeAdapter([ConnectionError("down")]),
                                            FakeAdapter(['```json\n{"answer": "ok"}\n```']))
        result = await providers.generate(REQUEST)
        self.assertEqual((result.provider, result.model, result.data), ("b", "m2", {"answer": "ok"}))
        self.assertEqual([event for event, _ in records], ["provider_failed", "provider_call"])

    async def test_invalid_or_truncated_output_tries_next_then_reports_invalid_output(self):
        providers, _ = self.providers(FakeAdapter(['{"answer": 1}']),
                                      FakeAdapter([AdapterResult('{"answer": "cut', truncated=True)]))
        with self.assertRaises(ModelError) as raised:
            await providers.generate(REQUEST)
        self.assertEqual(raised.exception.kind, "invalid_output")
        providers, _ = self.providers(FakeAdapter(["not json"]), FakeAdapter(['{"answer": "fine"}']))
        self.assertEqual((await providers.generate(REQUEST)).data, {"answer": "fine"})

    async def test_missing_keys_and_unknown_profiles_are_not_configured(self):
        providers = Providers({"x": {"kind": "openai_compatible", "api_key_env": "CORE_TEST_MISSING_KEY"}},
                              {"default": [{"provider": "x", "model": "m"}]})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CORE_TEST_MISSING_KEY", None)
            with self.assertRaises(ModelError) as raised:
                await providers.generate(ModelRequest([{"role": "user", "content": "hi"}]))
        self.assertEqual(raised.exception.kind, "not_configured")
        with self.assertRaises(ModelError) as raised:
            await providers.generate(ModelRequest([{"role": "user", "content": "hi"}], profile="nope"))
        self.assertEqual(raised.exception.kind, "not_configured")
        with self.assertRaises(ValueError):
            Providers({}, {"default": [{"provider": "ghost", "model": "m"}]})


class FakeOpenAIClient:
    def __init__(self, fail_effort=False):
        self.calls, self.fail_effort = [], fail_effort
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **options):
        self.options = options
        return self

    async def create(self, **params):
        self.calls.append(params)
        if self.fail_effort and "reasoning_effort" in params:
            raise ValueError("Unrecognized request argument supplied: reasoning_effort")
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="{}"))],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_compatible_native_schema_and_json_mode(self):
        client = FakeOpenAIClient()
        adapter = OpenAICompatible({"schema_mode": "native", "token_param": "max_completion_tokens",
                                    "effort_param": True}, client)
        result = await adapter.complete("gpt", REQUEST.messages, SCHEMA, "reply", "low", 100, 30)
        call = client.calls[0]
        self.assertEqual(call["response_format"]["json_schema"]["name"], "reply")
        self.assertEqual((call["max_completion_tokens"], call["reasoning_effort"]), (100, "low"))
        self.assertEqual((result.input_tokens, result.schema_mode, client.options["timeout"]), (3, "native", 30))
        client = FakeOpenAIClient()
        await OpenAICompatible({"schema_mode": "json_object"}, client).complete("ds", REQUEST.messages, SCHEMA,
                                                                                "reply", "low", 100, 30)
        call = client.calls[0]
        self.assertEqual(call["response_format"], {"type": "json_object"})
        self.assertIn("JSON schema", call["messages"][0]["content"])
        self.assertTrue(call["messages"][-1]["content"].endswith("(Reply with one JSON object.)"))
        self.assertEqual(REQUEST.messages[0]["content"], "sys")
        self.assertEqual(REQUEST.messages[-1]["content"], "hi")

    async def test_rejected_effort_parameter_is_retried_without_it_once_and_remembered(self):
        client = FakeOpenAIClient(fail_effort=True)
        adapter = OpenAICompatible({"effort_param": True}, client)
        await adapter.complete("m", [{"role": "user", "content": "hi"}], None, "x", "low", 50, 10)
        await adapter.complete("m", [{"role": "user", "content": "hi"}], None, "x", "low", 50, 10)
        self.assertEqual(["reasoning_effort" in call for call in client.calls], [True, False, False])

    async def test_anthropic_separates_system_and_reserves_thinking_tokens(self):
        captured = {}

        async def create(**params):
            captured.update(params)
            return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking="..."),
                                            SimpleNamespace(type="text", text='{"answer": "x"}')],
                                   usage=SimpleNamespace(input_tokens=4, output_tokens=6), stop_reason="end_turn")
        client = SimpleNamespace(with_options=lambda **options: SimpleNamespace(messages=SimpleNamespace(create=create)))
        result = await Anthropic({"adaptive_thinking": True, "thinking_reserve": 1000}, client).complete(
            "claude", REQUEST.messages, SCHEMA, "reply", "medium", 500, 30)
        self.assertEqual(captured["system"], "sys")
        self.assertEqual([m["role"] for m in captured["messages"]], ["user"])
        self.assertEqual(captured["max_tokens"], 1500)
        self.assertEqual(captured["output_config"]["effort"], "medium")
        self.assertEqual(captured["output_config"]["format"]["schema"], SCHEMA)
        self.assertEqual(result.text, '{"answer": "x"}')

    async def test_gemini_maps_roles_and_schema(self):
        captured = {}

        async def generate_content(**params):
            captured.update(params)
            return SimpleNamespace(text='{"answer": "y"}', candidates=[SimpleNamespace(
                finish_reason=SimpleNamespace(name="STOP"))], usage_metadata=SimpleNamespace(
                prompt_token_count=5, candidates_token_count=7))
        client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
        messages = [*REQUEST.messages, {"role": "assistant", "content": "a"}, {"role": "user", "content": "b"}]
        result = await Gemini({}, client).complete("gem", messages, SCHEMA, "reply", "low", 200, 30)
        self.assertEqual([content.role for content in captured["contents"]], ["user", "model", "user"])
        self.assertEqual(captured["config"].system_instruction, "sys")
        self.assertEqual(captured["config"].response_json_schema, SCHEMA)
        self.assertEqual((result.output_tokens, result.truncated), (7, False))


if __name__ == "__main__":
    unittest.main()

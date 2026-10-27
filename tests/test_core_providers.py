import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from contracts import ModelError, ModelRequest, ToolCall, ToolSpec
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

    async def test_a_rejected_answer_falls_back_with_its_reason_never_silently(self):
        providers, records = self.providers(FakeAdapter(["   "]), FakeAdapter(['{"answer": "ok"}']))
        result = await providers.generate(REQUEST)
        self.assertEqual((result.provider, result.fallbacks), ("b", ("a/m1: blank answer (not JSON)",)))
        self.assertIn(("provider_rejected", "blank answer (not JSON)"),
                      [(event, fields.get("reason")) for event, fields in records])
        providers, _ = self.providers(FakeAdapter([ConnectionError("down")]), FakeAdapter(['{"answer": "ok"}']))
        self.assertEqual((await providers.generate(REQUEST)).fallbacks, ("a/m1: ConnectionError: down",))
        providers, _ = self.providers(FakeAdapter(['{"answer": "ok"}']))
        self.assertEqual((await providers.generate(REQUEST)).fallbacks, ())  # the first one answered

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
        # Several objects (with words around them): the last one that fits the schema is the answer, as a model that
        # writes two usually corrects the first.
        providers, _ = self.providers(FakeAdapter(['{"answer": "first"}\n{"answer": "second"} and some words']))
        self.assertEqual((await providers.generate(REQUEST)).data, {"answer": "second"})
        providers, _ = self.providers(FakeAdapter(['{"answer": "x"} {"answer": 1}']))
        self.assertEqual((await providers.generate(REQUEST)).data, {"answer": "x"})  # the later one does not fit
        providers, _ = self.providers(FakeAdapter(['{"answer": 1} {"answer": 2}']))
        with self.assertRaises(ModelError):
            await providers.generate(REQUEST)  # none fits: still invalid

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
        self.assertFalse(providers.ready("x"))  # still not ready after a failed call made (and cached) its adapter
        self.assertFalse(providers.profile_ready("default"))
        with self.assertRaises(ValueError):
            Providers({}, {"default": [{"provider": "ghost", "model": "m"}]})

    async def test_listed_models_are_narrowed_by_the_providers_settings(self):
        import datetime as dt

        class Lister(FakeAdapter):
            async def list_models(self):
                return [("gpt-6", dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc)), ("gpt-4o", dt.date(2024, 5, 13)),
                        ("gpt-image-3", dt.date(2026, 9, 4)), ("undated-model", None), "plain-id"]

        settings = {"models_since": dt.date(2026, 6, 1), "models_exclude": ["image"]}  # as YAML reads them
        providers = Providers({"x": settings, "y": {**settings, "models_since": "2026-06-01", "models_allow": ["^gpt"]}},
                              {"default": [{"provider": "x", "model": "gpt-6"}]},
                              adapters={"x": Lister(), "y": Lister()})
        self.assertEqual(await providers.list_models("x"), ["gpt-6", "plain-id", "undated-model"])
        self.assertEqual(await providers.list_models("y"), ["gpt-6"])

    def test_the_key_comes_from_its_env_variable_else_from_the_settings(self):
        from core.providers.adapters import NotConfigured, api_key
        settings = {"api_key_env": "CORE_TEST_KEY", "api_key": "written-in-config"}
        with patch.dict(os.environ, {"CORE_TEST_KEY": "from-env"}):
            self.assertEqual(api_key(settings), "from-env")
        with patch.dict(os.environ, {}):
            os.environ.pop("CORE_TEST_KEY", None)
            self.assertEqual(api_key(settings), "written-in-config")
            with self.assertRaises(NotConfigured):
                api_key({"api_key_env": "CORE_TEST_KEY"})
            providers = Providers({"x": {"kind": "openai_compatible", **settings}},
                                  {"default": [{"provider": "x", "model": "m"}]})
            self.assertEqual(providers.configured(), ["x"])
        with patch.dict(os.environ, {"CORE_TEST_KEY": "disabled"}):  # switched off: not offered, not counted
            providers = Providers({"x": {"kind": "openai_compatible", "api_key_env": "CORE_TEST_KEY"}},
                                  {"default": [{"provider": "x", "model": "m"}]})
            self.assertEqual((providers.configured(), providers.available()), ([], []))


class Status(Exception):
    """A provider's HTTP error, as the SDKs raise them (`status_code`)."""

    def __init__(self, status: int):
        super().__init__(f"status {status}")
        self.status_code = status


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def club(self, first, second):
        recorded = []
        providers = Providers({"a": {"kind": "openai_compatible"}, "b": {"kind": "openai_compatible"}},
                              {"default": [{"provider": "a", "model": "m"}, {"provider": "b", "model": "n"}]},
                              adapters={"a": first, "b": second}, record=lambda event, **fields: recorded.append(event))
        return providers, recorded

    async def test_a_rate_limit_is_asked_again_before_the_next_candidate(self):
        first, second = FakeAdapter([Status(429), Status(503), {"answer": "ok"}]), FakeAdapter([{"answer": "other"}])
        providers, recorded = self.club(first, second)
        with patch("core.providers.registry.RETRY_DELAYS_S", (0.0, 0.0)):
            result = await providers.generate(REQUEST)
        self.assertEqual((result.data, len(first.calls), len(second.calls)), ({"answer": "ok"}, 3, 0))
        self.assertEqual(recorded.count("provider_retry"), 2)

    async def test_a_refusal_or_a_lasting_failure_goes_to_the_next_candidate(self):
        for failures in ([Status(402)], [Status(429)] * 3):  # no balance: at once; still limited: after two pauses
            first, second = FakeAdapter(failures), FakeAdapter([{"answer": "other"}])
            providers, recorded = self.club(first, second)
            with patch("core.providers.registry.RETRY_DELAYS_S", (0.0, 0.0)):
                result = await providers.generate(REQUEST)
            self.assertEqual((result.data, len(first.calls)), ({"answer": "other"}, len(failures)))


TOOLS = (ToolSpec("run", "Run a line.", {"type": "object", "properties": {"command": {"type": "string"}},
                                            "required": ["command"], "additionalProperties": False}),)


class FakeOpenAIClient:
    def __init__(self, fail_effort=False, contents=()):
        self.calls, self.fail_effort, self.contents = [], fail_effort, list(contents)  # contents: answers in turn
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **options):
        self.options = options
        return self

    async def create(self, **params):
        self.calls.append(params)
        if self.fail_effort and "reasoning_effort" in params:
            raise ValueError("Unrecognized request argument supplied: reasoning_effort")
        content = self.contents.pop(0) if self.contents else "{}"
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_blank_json_mode_answer_is_asked_once_more(self):
        client = FakeOpenAIClient(contents=["   ", '{"answer": "ok"}'])
        result = await OpenAICompatible({}, client).complete("deepseek-flash", REQUEST.messages, SCHEMA, "reply",
                                                             "low", 100, 30)
        self.assertEqual((result.text, len(client.calls), result.output_tokens), ('{"answer": "ok"}', 2, 4))
        twice = FakeOpenAIClient(contents=["   ", "  "])  # still blank: reported as it is (the registry moves on)
        result = await OpenAICompatible({}, twice).complete("deepseek-flash", REQUEST.messages, SCHEMA, "reply",
                                                            "low", 100, 30)
        self.assertEqual((result.text.strip(), len(twice.calls)), ("", 2))

    async def test_inline_history_folds_earlier_turns_for_json_requests_only(self):
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "hello"}, {"role": "user", "content": "and now?"}]
        client = FakeOpenAIClient()
        adapter = OpenAICompatible({"inline_history": True}, client)
        await adapter.complete("deepseek-flash", history, SCHEMA, "reply", "low", 100, 30)
        sent = client.calls[0]["messages"]
        self.assertEqual([message["role"] for message in sent], ["system", "user"])
        self.assertIn("hi\n\n(Your earlier reply:) hello\n\nand now?", sent[1]["content"])
        await adapter.complete("deepseek-flash", history, None, "reply", "low", 100, 30)  # plain text: as it was
        self.assertEqual([message["role"] for message in client.calls[1]["messages"]],
                         ["system", "user", "assistant", "user"])
        plain = FakeOpenAIClient()
        await OpenAICompatible({}, plain).complete("gpt", history, SCHEMA, "reply", "low", 100, 30)
        self.assertIn("assistant", [message["role"] for message in plain.calls[0]["messages"]])

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

    async def test_strict_tool_mode_sends_the_schema_as_one_strict_tool_and_reads_its_arguments(self):
        schema = {"type": "object", "properties": {
            "note": {"type": "string", "maxLength": 9},
            "actions": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
                "tool": {"type": "string", "enum": ["run"]}, "arguments": {"type": "object"}},
                "required": ["tool", "arguments"], "additionalProperties": False}}},
            "required": [], "additionalProperties": False}
        arguments = json.dumps({"note": "", "actions": [{"tool": "run", "arguments": json.dumps({"command": "ls"})}]})
        call = SimpleNamespace(function=SimpleNamespace(name="turn", arguments=arguments))

        class Client(FakeOpenAIClient):
            async def create(self, **params):
                self.calls.append(params)
                message = SimpleNamespace(content="", tool_calls=[call] if len(self.calls) > 1 else [])
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
                                       usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
        client = Client()
        result = await OpenAICompatible({"schema_mode": "strict_tool"}, client).complete(
            "ds", REQUEST.messages, schema, "turn", "high", 100, 30)
        tool = client.calls[0]["tools"][0]["function"]
        self.assertEqual((tool["name"], tool["strict"], client.calls[0]["tool_choice"]), ("turn", True, "auto"))
        sent = tool["parameters"]
        self.assertEqual(sent["required"], ["note", "actions"])  # every property, as strict mode demands
        self.assertNotIn("maxLength", sent["properties"]["note"])
        self.assertNotIn("maxItems", sent["properties"]["actions"])
        self.assertEqual(sent["properties"]["actions"]["items"]["properties"]["arguments"]["type"], "string")
        self.assertNotIn("response_format", client.calls[0])
        self.assertTrue(client.calls[0]["messages"][-1]["content"].endswith("(Answer by calling turn.)"))
        self.assertEqual(REQUEST.messages[-1]["content"], "hi")
        self.assertEqual(len(client.calls), 2)  # no tool call and no text: asked once more, as JSON mode is
        self.assertEqual(json.loads(result.text), {"note": "", "actions": [{"tool": "run", "arguments": {"command": "ls"}}]})
        self.assertEqual((result.schema_mode, result.output_tokens), ("strict_tool", 4))
        from core.providers.adapters import from_strict_tool
        loose = schema["properties"]["actions"]
        self.assertEqual(from_strict_tool([{"tool": "run", "arguments": '{"command": "ls"}]'}], loose),
                         [{"tool": "run", "arguments": {"command": "ls"}}])  # a stray bracket after the object
        self.assertEqual(from_strict_tool([{"tool": "run", "arguments": '["ls"]'}], loose),
                         [{"tool": "run", "arguments": '["ls"]'}])  # not an object: left for the registry to refuse

    async def test_rejected_effort_parameter_is_retried_without_it_once_and_remembered(self):
        client = FakeOpenAIClient(fail_effort=True)
        adapter = OpenAICompatible({"effort_param": True}, client)
        await adapter.complete("m", [{"role": "user", "content": "hi"}], None, "x", "low", 50, 10)
        await adapter.complete("m", [{"role": "user", "content": "hi"}], None, "x", "low", 50, 10)
        self.assertEqual(["reasoning_effort" in call for call in client.calls], [True, False, False])

    async def test_a_model_that_takes_tools_only_without_reasoning_gets_none_for_its_tool_steps_alone(self):
        client = FakeOpenAIClient()
        create = client.create

        async def picky(**params):  # GPT-6 Luna on Chat Completions
            if params["model"] == "gpt-6-luna" and params.get("tools") and \
                    params.get("reasoning_effort") not in (None, "none"):
                client.calls.append(params)
                raise ValueError("Error code: 400 - Function tools with reasoning_effort are not supported for "
                                 "gpt-6-luna in /v1/chat/completions. To use function tools, use /v1/responses or "
                                 "set reasoning_effort to 'none'.")
            return await create(**params)
        client.chat.completions.create = picky
        adapter = OpenAICompatible({"effort_param": True}, client)
        hi = [{"role": "user", "content": "hi"}]
        await adapter.complete("gpt-6-luna", hi, None, "turn", "high", 50, 10, tools=TOOLS)
        await adapter.complete("gpt-6-luna", hi, None, "turn", "high", 50, 10, tools=TOOLS)
        await adapter.complete("gpt-6-luna", hi, SCHEMA, "reply", "low", 50, 10)  # no tools: its effort stays
        await adapter.complete("deepseek-flash", hi, None, "turn", "high", 50, 10, tools=TOOLS)
        self.assertEqual([call.get("reasoning_effort") for call in client.calls], ["high", "none", "none", "low", "high"])

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

    def test_claude_schema_drops_limits_it_rejects_but_keeps_structure(self):
        from core.providers.adapters import claude_schema
        schema = {"type": "object", "properties": {
            "actions": {"type": "array", "maxItems": 5, "minItems": 2, "items": {"type": "string", "maxLength": 9}},
            "importance": {"type": "integer", "minimum": 1, "maximum": 5}, "status": {"enum": ["a", "b"]}},
            "required": ["actions"], "additionalProperties": False}
        self.assertEqual(claude_schema(schema), {"type": "object", "properties": {
            "actions": {"type": "array", "items": {"type": "string"}}, "importance": {"type": "integer"},
            "status": {"enum": ["a", "b"]}}, "required": ["actions"], "additionalProperties": False})

    async def test_claude_asks_in_the_prompt_when_the_schema_has_open_objects(self):
        captured = {}

        async def create(**params):
            captured.update(params)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"tool": "x", "arguments": {}}')],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_reason="end_turn")
        client = SimpleNamespace(with_options=lambda **options: SimpleNamespace(messages=SimpleNamespace(create=create)))
        schema = {"type": "object", "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
                  "required": ["tool", "arguments"], "additionalProperties": False}
        result = await Anthropic({}, client).complete("claude", REQUEST.messages, schema, "turn", "low", 100, 30)
        self.assertNotIn("output_config", captured)
        self.assertIn("JSON schema", captured["system"])
        self.assertEqual(result.schema_mode, "instruction")

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


    async def test_images_go_to_candidates_that_see_and_others_get_a_note(self):
        import tempfile
        from pathlib import Path
        from contracts import FileRef
        with tempfile.TemporaryDirectory() as directory:
            png = Path(directory) / "a.png"
            png.write_bytes(b"\x89PNG fake")
            other = Path(directory) / "notes.txt"
            other.write_text("x")
            blind, seeing = FakeAdapter(["text only"]), FakeAdapter([ConnectionError("down")])
            providers = Providers({"blind": {"vision": False}}, {"default": [
                {"provider": "blind", "model": "b"}, {"provider": "seeing", "model": "s"}]},
                adapters={"blind": blind, "seeing": seeing})
            request = ModelRequest([{"role": "user", "content": "what is this?"}],
                                   images=(FileRef(str(png), "a.png", "image/png"), FileRef(str(other), "notes.txt")))
            result = await providers.generate(request)
        self.assertEqual(result.text, "text only")
        self.assertEqual([image.mime for image in seeing.calls[0]["images"]], ["image/png"])
        self.assertEqual(blind.calls[0]["images"], [])
        self.assertIn("1 image(s) attached, but you cannot see images", blind.calls[0]["messages"][-1]["content"])
        self.assertEqual(request.messages[-1]["content"], "what is this?")

    async def test_each_adapter_attaches_images_to_the_last_user_message(self):
        from core.providers.adapters import Image
        image = Image("image/png", b"png")
        client = FakeOpenAIClient()
        await OpenAICompatible({}, client).complete("m", REQUEST.messages, None, "x", "low", 50, 10, images=[image])
        parts = client.calls[0]["messages"][-1]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "hi"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64,cG5n")
        captured = {}

        async def create(**params):
            captured.update(params)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")],
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1), stop_reason="end_turn")
        anthropic = SimpleNamespace(with_options=lambda **o: SimpleNamespace(messages=SimpleNamespace(create=create)))
        await Anthropic({}, anthropic).complete("c", REQUEST.messages, None, "x", "low", 50, 10, images=[image])
        content = captured["messages"][-1]["content"]
        self.assertEqual((content[0]["source"]["data"], content[1]["text"]), ("cG5n", "hi"))
        gemini_calls = {}

        async def generate_content(**params):
            gemini_calls.update(params)
            return SimpleNamespace(text="ok", candidates=[], usage_metadata=None)
        gemini = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
        await Gemini({}, gemini).complete("g", REQUEST.messages, None, "x", "low", 50, 10, images=[image])
        parts = gemini_calls["contents"][-1].parts
        self.assertEqual((parts[0].text, parts[1].inline_data.mime_type), ("hi", "image/png"))

    async def test_min_interval_spaces_requests_to_one_provider(self):
        import time
        adapter = FakeAdapter(["a", "b"])
        providers = Providers({"g": {"min_interval_s": 0.2}}, {"default": [{"provider": "g", "model": "m"}]},
                              adapters={"g": adapter})
        started = time.monotonic()
        await providers.generate(ModelRequest([{"role": "user", "content": "1"}]))
        await providers.generate(ModelRequest([{"role": "user", "content": "2"}]))
        self.assertGreaterEqual(time.monotonic() - started, 0.19)


class ToolAdapter:
    """A provider that calls tools: answers in turn, each (text, [(name, arguments)])."""

    supports_tools = True

    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=(),
                       tools=()):
        self.calls.append({"messages": messages, "tools": tools, "schema": schema})
        if isinstance(self.answers[0], Exception):
            raise self.answers.pop(0)
        text, calls = self.answers.pop(0)
        return AdapterResult(text, 10, 5, tool_calls=tuple(ToolCall(f"c{index}", name, arguments)
                                                           for index, (name, arguments) in enumerate(calls)),
                             replay={"reasoning_content": "thought"})


class ToolCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_calls_go_out_as_functions_and_come_back_with_their_replay(self):
        call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="run", arguments='{"command": "ls"}]'))

        class Client(FakeOpenAIClient):
            async def create(self, **params):
                self.calls.append(params)
                message = SimpleNamespace(content="Looking.", tool_calls=[call], reasoning_content="I should list.")
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
                                       usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
        client = Client()
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "", "replay": {"reasoning_content": "earlier"},
                    "tool_calls": [{"id": "call_0", "name": "run", "arguments": {"command": "pwd"}}]},
                   {"role": "tool", "tool_call_id": "call_0", "content": "/work"}]
        result = await OpenAICompatible({"schema_mode": "json_object", "inline_history": True}, client).complete(
            "ds", history, None, "turn", "high", 100, 30, tools=TOOLS)
        sent = client.calls[0]
        self.assertEqual((sent["tools"][0]["function"]["name"], sent["tool_choice"]), ("run", "auto"))
        self.assertNotIn("response_format", sent)
        self.assertEqual(sent["messages"][2], {"role": "assistant", "content": "", "reasoning_content": "earlier",
                                               "tool_calls": [{"id": "call_0", "type": "function", "function": {
                                                   "name": "run", "arguments": '{"command": "pwd"}'}}]})
        self.assertEqual(sent["messages"][3], {"role": "tool", "tool_call_id": "call_0", "content": "/work"})
        self.assertEqual(result.tool_calls, (ToolCall("call_1", "run", {"command": "ls"}),))  # stray bracket dropped
        self.assertEqual((result.text, result.replay, result.schema_mode),
                         ("Looking.", {"reasoning_content": "I should list."}, "tools"))

    async def test_a_blank_tool_answer_is_asked_once_more(self):
        class Client(FakeOpenAIClient):
            async def create(self, **params):
                self.calls.append(params)
                calls = [] if len(self.calls) == 1 else [SimpleNamespace(
                    id="c", function=SimpleNamespace(name="run", arguments='{"command": "ls"}'))]
                message = SimpleNamespace(content="", tool_calls=calls)
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=message)],
                                       usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
        client = Client()
        result = await OpenAICompatible({}, client).complete("ds", REQUEST.messages, None, "turn", "low", 100, 30,
                                                             tools=TOOLS)
        self.assertEqual((len(client.calls), result.tool_calls[0].name, result.replay), (2, "run", None))

    async def test_tool_calls_are_checked_against_their_tools(self):
        request = ModelRequest([{"role": "user", "content": "go"}], tools=TOOLS)
        good = ToolAdapter([("", [("run", {"command": "ls"})])])
        providers = Providers({"calls": {"kind": "openai_compatible"}}, {"default": [{"provider": "calls", "model": "c"}]},
                              adapters={"calls": good})
        result = await providers.generate(request)
        self.assertEqual((result.provider, result.tool_calls[0].arguments, result.replay),
                         ("calls", {"command": "ls"}, {"provider": "calls", "fields": {"reasoning_content": "thought"}}))
        self.assertEqual(good.calls[0]["tools"], TOOLS)
        for answer, problem in (([("run", {})], "does not match its parameters"), ([("rm", {})], "not one of its tools"),
                                ([("run", '{"command": ')], "not a JSON object"), ([], "blank answer")):
            bad = Providers({"calls": {"kind": "openai_compatible"}}, {"default": [{"provider": "calls", "model": "c"}]},
                            adapters={"calls": ToolAdapter([("", answer)])})
            with self.assertRaises(ModelError) as caught:
                await bad.generate(request)
            self.assertEqual(caught.exception.kind, "invalid_output")
            self.assertIn(problem, str(caught.exception))

    async def test_a_candidate_that_cannot_call_tools_ends_the_attempt_in_its_place(self):
        """The profile's order holds: a later candidate never answers instead of one that cannot call tools."""
        request = ModelRequest([{"role": "user", "content": "go"}], tools=TOOLS)

        def providers(order, down=()):
            adapters = {"plain": FakeAdapter([{"answer": "x"}]),
                        "calls": ToolAdapter([RuntimeError("down")] if "calls" in down else
                                             [("", [("run", {"command": "ls"})])])}
            return Providers({name: {"kind": "openai_compatible"} for name in adapters},
                             {"default": [{"provider": name, "model": name[0]} for name in order]}, adapters=adapters)
        self.assertEqual((await providers(["calls", "plain"]).generate(request)).provider, "calls")
        for order, down, said in ((["plain", "calls"], (), "plain/p cannot call tools"),
                                  (["calls", "plain"], ("calls",), "before it: calls/c: RuntimeError")):
            with self.assertRaises(ModelError) as caught:
                await providers(order, down).generate(request)
            self.assertEqual(caught.exception.kind, "tools_unsupported")
            self.assertIn(said, str(caught.exception))
        with self.assertRaises(ModelError) as caught:  # all can call tools and none answered: as ever
            await providers(["calls"], ("calls",)).generate(request)
        self.assertEqual(caught.exception.kind, "unavailable")
        chosen = providers(["calls"])  # a model chosen with $model that cannot call tools: asked in JSON instead
        with self.assertRaises(ModelError) as caught:
            await chosen.generate(request, first={"provider": "plain", "model": "p"})
        self.assertEqual(caught.exception.kind, "tools_unsupported")

    async def test_every_call_is_answered_right_after_it_whatever_order_the_engine_wrote(self):
        call = lambda id, name="run": {"id": id, "name": name, "arguments": {"command": "x"}}
        request = ModelRequest([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [call("a"), call("b"), call("c")]},
            {"role": "user", "content": "[tool run] waiting for the owner's approval"},
            {"role": "tool", "tool_call_id": "b", "content": "B"},
            {"role": "tool", "tool_call_id": "a", "content": "A"},
            {"role": "tool", "tool_call_id": "zzz", "content": "answers no call"},
            {"role": "user", "content": "and then?"}], tools=TOOLS)
        adapter = ToolAdapter([("Done.", [])])
        await Providers({"calls": {"kind": "openai_compatible"}}, {"default": [{"provider": "calls", "model": "c"}]},
                        adapters={"calls": adapter}).generate(request)
        sent = adapter.calls[0]["messages"]
        self.assertEqual([(m["role"], m.get("tool_call_id") or m.get("content")) for m in sent], [
            ("user", "go"), ("assistant", ""), ("tool", "a"), ("tool", "b"), ("tool", "c"),
            ("user", "[tool run] waiting for the owner's approval"), ("user", "and then?")])
        self.assertEqual(sent[4]["content"], "[tool run] This call did not run.")

    async def test_dotted_tool_names_reach_the_api_as_it_allows_and_come_back_as_they_were(self):
        call = SimpleNamespace(id="call_9", function=SimpleNamespace(name="web__search", arguments='{"command": "q"}'))

        class Client(FakeOpenAIClient):
            async def create(self, **params):
                self.calls.append(params)
                message = SimpleNamespace(content="", tool_calls=[call])
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls", message=message)],
                                       usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2))
        tools = (ToolSpec("web.search", "Search.", TOOLS[0].parameters), ToolSpec("web__search", "Other.", {}),
                 ToolSpec("a" * 70 + ".b", "Long.", {}))
        history = [{"role": "user", "content": "hi"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "name": "web.search", "arguments": {}}]},
                   {"role": "tool", "tool_call_id": "c0", "content": "found"}]
        client = Client()
        result = await OpenAICompatible({}, client).complete("ds", history, None, "turn", "low", 100, 30, tools=tools)
        names = [tool["function"]["name"] for tool in client.calls[0]["tools"]]
        self.assertEqual(names[:2], ["web__search", "web__search_2"])
        self.assertTrue(all(len(name) <= 64 and name.replace("_", "").replace("-", "").isalnum() for name in names))
        self.assertEqual(client.calls[0]["messages"][1]["tool_calls"][0]["function"]["name"], "web__search")
        self.assertEqual(result.tool_calls[0].name, "web.search")

    async def test_images_go_with_the_last_user_message_when_tool_results_follow_it(self):
        from contracts.models import Image
        client = FakeOpenAIClient(contents=["ok"])
        history = [{"role": "user", "content": "look"},
                   {"role": "assistant", "content": "", "tool_calls": [{"id": "c0", "name": "run", "arguments": {}}]},
                   {"role": "tool", "tool_call_id": "c0", "content": "done"}]
        await OpenAICompatible({}, client).complete("gpt", history, None, "turn", "low", 100, 30,
                                                    images=[Image("image/png", b"ABC")], tools=TOOLS)
        sent = client.calls[0]["messages"]
        self.assertEqual(sent[0]["content"][1]["type"], "image_url")
        self.assertEqual(sent[-1], {"role": "tool", "tool_call_id": "c0", "content": "done"})

    async def test_a_replay_goes_back_only_to_the_provider_that_wrote_it_and_never_overwrites_the_message(self):
        from core.providers.adapters import openai_messages
        replay = {"provider": "deepseek", "fields": {"reasoning_content": "mine", "role": "user", "content": "X"}}
        history = [{"role": "user", "content": "go"},
                   {"role": "assistant", "content": "", "replay": replay,
                    "tool_calls": [{"id": "c1", "name": "run", "arguments": {"command": "x"}}]},
                   {"role": "tool", "tool_call_id": "c1", "content": "done"}]
        for provider, expected in (("deepseek", "mine"), ("openai", None)):
            adapter = ToolAdapter([("ok", [])])
            await Providers({provider: {"kind": "openai_compatible"}}, {"default": [{"provider": provider, "model": "m"}]},
                            adapters={provider: adapter}).generate(ModelRequest(history, tools=TOOLS))
            sent = openai_messages(adapter.calls[0]["messages"])[1]
            self.assertEqual((sent.get("reasoning_content"), sent["role"], sent["content"]), (expected, "assistant", ""))

    async def test_a_candidate_without_a_key_is_passed_over_as_ever_not_ended_on(self):
        request = ModelRequest([{"role": "user", "content": "go"}], tools=TOOLS)
        providers = Providers({"gemini": {"kind": "gemini", "api_key_env": "NO_SUCH_KEY_FOR_TESTS"},
                               "calls": {"kind": "openai_compatible"}},
                              {"default": [{"provider": "gemini", "model": "g"}, {"provider": "calls", "model": "c"}]},
                              adapters={"calls": ToolAdapter([("", [("run", {"command": "ls"})])])})
        result = await providers.generate(request)
        self.assertEqual(result.provider, "calls")
        self.assertIn("gemini/g: not configured", result.fallbacks[0])

    async def test_parsed_arguments_and_missing_ids_are_read(self):
        from core.providers.adapters import openai_calls
        message = SimpleNamespace(tool_calls=[
            SimpleNamespace(id="", function=SimpleNamespace(name="run", arguments={"command": "a"})),
            SimpleNamespace(id=None, function=SimpleNamespace(name="run", arguments='{"command": "b"}'))])
        first, second = openai_calls(message)
        self.assertEqual((first.arguments, second.arguments), ({"command": "a"}, {"command": "b"}))
        self.assertTrue(first.id and second.id and first.id != second.id)

    async def test_requests_without_tools_place_images_and_notes_as_before(self):
        from contracts.models import Image
        client = FakeOpenAIClient(contents=['{"answer": "ok"}'])
        history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "look"},
                   {"role": "assistant", "content": "seen"}]
        await OpenAICompatible({}, client).complete("gpt", history, SCHEMA, "reply", "low", 100, 30,
                                                    images=[Image("image/png", b"ABC")])
        self.assertEqual([m["content"] for m in client.calls[0]["messages"][1:]], ["look", "seen"])  # untouched

    async def test_requests_without_tools_are_unchanged(self):
        client = FakeOpenAIClient(contents=['{"answer": "ok"}'])
        result = await OpenAICompatible({}, client).complete("ds", REQUEST.messages, SCHEMA, "reply", "low", 100, 30)
        self.assertNotIn("tools", client.calls[0])
        self.assertEqual((result.tool_calls, result.replay, result.schema_mode), ((), None, "json_object"))



class SettleToolMessagesTests(unittest.TestCase):
    def test_any_order_of_messages_settles_into_one_the_apis_accept(self):
        import random
        from core.providers.registry import settle_tool_messages
        for seed in range(300):
            rng = random.Random(seed)
            messages, open_calls, number = [{"role": "user", "content": "start"}], [], 0
            for _ in range(rng.randint(1, 14)):
                roll = rng.random()
                if roll < 0.3:
                    ids = [f"c{number + index}" for index in range(rng.randint(1, 3))]
                    number += len(ids)
                    messages.append({"role": "assistant", "content": "",
                                     "tool_calls": [{"id": id, "name": "run", "arguments": {}} for id in ids]})
                    open_calls += ids
                elif roll < 0.6 and open_calls:
                    id = open_calls.pop(rng.randrange(len(open_calls)))
                    messages.append({"role": "tool", "tool_call_id": id, "content": f"result {id}"})
                elif roll < 0.7:
                    messages.append({"role": "tool", "tool_call_id": f"stray{seed}", "content": "answers nothing"})
                else:
                    messages.append({"role": rng.choice(["user", "assistant"]), "content": f"text {roll:.3f}"})
            settled = settle_tool_messages(messages)
            for index, message in enumerate(settled):
                if message.get("tool_calls"):
                    ids = [call["id"] for call in message["tool_calls"]]
                    after = settled[index + 1:index + 1 + len(ids)]
                    self.assertEqual([item.get("tool_call_id") for item in after], ids, seed)
                    self.assertTrue(all(item["role"] == "tool" for item in after), seed)
            asked = {call["id"] for m in settled for call in m.get("tool_calls") or ()}
            answered = [m["tool_call_id"] for m in settled if m["role"] == "tool"]
            self.assertEqual(sorted(answered), sorted(asked), seed)  # each call once, no strays
            kept = [m for m in messages if m["role"] != "tool"]
            self.assertEqual([m for m in settled if m["role"] != "tool"], kept, seed)  # the rest in its order
            for m in settled:  # a real result is kept as it was
                if m["role"] == "tool" and not m["content"].endswith("did not run."):
                    self.assertEqual(m["content"], f"result {m['tool_call_id']}", seed)

if __name__ == "__main__":
    unittest.main()

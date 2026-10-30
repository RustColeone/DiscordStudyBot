"""Thin provider adapters, the built-in kinds. Each turns one model call into one API request and reports raw
text. Modules may add kinds of their own (contracts.ModelAdapter, Contributions.adapters)."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts.models import AdapterResult, Image, NotConfigured, ToolCall

IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
               ".webp": "image/webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 6
LIST_TIMEOUT_S = 15.0  # asking a provider which models it offers


def load_images(files) -> list[Image]:
    """Readable supported images, at most MAX_IMAGES; others are skipped."""
    images = []
    for file in list(files)[-MAX_IMAGES:]:
        path = Path(file.path)
        mime = file.mime if file.mime in IMAGE_TYPES.values() else IMAGE_TYPES.get(path.suffix.lower(), "")
        try:
            if mime and path.is_file() and path.stat().st_size <= MAX_IMAGE_BYTES:
                images.append(Image(mime, path.read_bytes()))
        except OSError:
            continue
    return images


def schema_instruction(schema: Mapping[str, Any]) -> str:
    return ("Respond with exactly one JSON object, no prose or code fences, matching this JSON schema:\n"
            + json.dumps(schema, ensure_ascii=False))


def with_schema_instruction(messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any]) -> list[dict]:
    instruction = schema_instruction(schema)
    result = [dict(message) for message in messages]
    if result and result[0].get("role") == "system":
        result[0]["content"] = result[0]["content"] + "\n\n" + instruction
    else:
        result.insert(0, {"role": "system", "content": instruction})
    # DeepSeek's JSON mode answers only whitespace after an assistant JSON turn unless the final
    # message asks for the JSON object again (reproduced: 0/3 valid without, 3/3 with).
    if result[-1].get("role") == "user":
        result[-1]["content"] = result[-1]["content"] + "\n\n(Reply with one JSON object.)"
    return result


def inline_history(messages: Sequence[Mapping[str, str]]) -> list[dict]:
    """The conversation as one user message, earlier assistant turns written into it (`inline_history`).
    DeepSeek's JSON mode often answers only whitespace after an assistant turn (reproduced: 1 in 3 valid as
    turns, 3 in 3 folded), so its JSON requests are sent this way."""
    if not any(message.get("role") == "assistant" for message in messages):
        return [dict(message) for message in messages]
    parts = [f"(Your earlier reply:) {message['content']}" if message["role"] == "assistant" else message["content"]
             for message in messages if message["role"] != "system"]
    return [*(dict(message) for message in messages if message["role"] == "system"),
            {"role": "user", "content": "\n\n".join(parts)}]


MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def function_name(name: str) -> str:
    """A tool's name as OpenAI-style APIs allow it: letters, digits, `_` and `-`, at most 64 (dots become `__`)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name.replace(".", "__"))[:64] or "tool"


def function_names(tools) -> dict[str, str]:
    """Each tool's API name, distinct even when two names map alike (`web.search` and `web__search`)."""
    names: dict[str, str] = {}
    for tool in tools:
        base, name, number = function_name(tool.name), function_name(tool.name), 2
        while name in names.values():
            name, number = f"{base[:60]}_{number}", number + 1
        names[tool.name] = name
    return names


STRICT_UNSUPPORTED = {"maxItems", "minItems", "uniqueItems", "minimum", "maximum", "exclusiveMinimum",
                      "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern", "format"}


def strict_tool_schema(schema: Any) -> Any:
    """A schema as strict tool calls take it (`schema_mode: strict_tool`; DeepSeek's beta): every property
    required, an object without properties sent as a JSON string (`from_strict_tool` decodes it), limits dropped.
    The registry still validates the reply against the full schema."""
    if not isinstance(schema, Mapping):
        return schema
    if schema.get("type") == "object" and not schema.get("properties"):
        return {"type": "string", "description": "a JSON object, written as a string"}
    result = {key: value for key, value in schema.items() if key not in STRICT_UNSUPPORTED}
    if isinstance(schema.get("items"), Mapping):
        result["items"] = strict_tool_schema(schema["items"])
    for key in ("anyOf", "oneOf"):
        if key in schema:
            result[key] = [strict_tool_schema(item) for item in schema[key]]
    if "properties" in schema:
        result["properties"] = {name: strict_tool_schema(value) for name, value in schema["properties"].items()}
        result["required"] = list(schema["properties"])
        result["additionalProperties"] = False
    return result


def from_strict_tool(value: Any, schema: Any) -> Any:
    """A strict tool call's arguments as the schema wants them: the strings standing for objects decoded again."""
    if not isinstance(schema, Mapping):
        return value
    if schema.get("type") == "object" and not schema.get("properties"):
        if not isinstance(value, str):
            return value
        try:  # the first JSON value in it: models sometimes leave a stray bracket after the object
            decoded, _ = json.JSONDecoder().raw_decode(value.strip())
        except ValueError:
            return value  # left as it is: the registry reports it, and the caller asks again
        return decoded if isinstance(decoded, dict) else value
    if isinstance(value, dict) and isinstance(schema.get("properties"), Mapping):
        return {key: from_strict_tool(item, schema["properties"].get(key)) for key, item in value.items()}
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        return [from_strict_tool(item, schema["items"]) for item in value]
    return value


def tool_answer(message: Any, name: str, schema: Mapping[str, Any]) -> str:
    """The JSON text of a strict tool call named `name`, else the message's own text (some replies come as text)."""
    for call in getattr(message, "tool_calls", None) or ():
        function = getattr(call, "function", None)
        if getattr(function, "name", "") == name:
            try:
                return json.dumps(from_strict_tool(json.loads(function.arguments), schema), ensure_ascii=False)
            except (TypeError, ValueError):
                return str(getattr(function, "arguments", "") or "")
    return getattr(message, "content", None) or ""


def openai_messages(messages: Sequence[Mapping[str, Any]], names: Mapping[str, str] | None = None) -> list[dict]:
    """The core's tool-call messages in OpenAI's form: an assistant message's `tool_calls` as function calls (under
    their API `names`), with its `replay` keys (DeepSeek's reasoning_content) beside them, and `tool` messages
    paired by `tool_call_id`."""
    names = names or {}
    result = []
    for message in messages:
        if message.get("role") == "assistant" and (message.get("tool_calls") or message.get("replay")):
            item: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            if message.get("tool_calls"):
                item["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": names.get(call["name"]) or function_name(call["name"]),
                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False)}}
                    for call in message["tool_calls"]]
            item.update({key: value for key, value in dict(message.get("replay") or {}).items()
                         if key not in MESSAGE_KEYS})  # (a replay never overwrites the message itself)
            result.append(item)
        elif message.get("role") == "tool":
            result.append({"role": "tool", "tool_call_id": message["tool_call_id"],
                           "content": message.get("content") or ""})
        else:
            result.append({"role": message["role"], "content": message.get("content") or ""})
    return result


def openai_calls(message: Any, names: Mapping[str, str] | None = None) -> tuple[ToolCall, ...]:
    """The function calls of an OpenAI-style message, under the tools' own names (`names`: API name → name);
    arguments that are not one JSON object stay a string."""
    names = names or {}
    calls = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or ()):
        function = getattr(call, "function", None)
        given = getattr(function, "arguments", "")
        raw = given if isinstance(given, Mapping) else str(given or "").strip() or "{}"  # (some servers parse it)
        try:  # the first JSON value in it: models sometimes leave a stray bracket after the object
            arguments = dict(raw) if isinstance(raw, Mapping) else json.JSONDecoder().raw_decode(raw)[0]
        except ValueError:
            arguments = raw
        name = str(getattr(function, "name", ""))
        calls.append(ToolCall(str(getattr(call, "id", "") or f"call_{uuid.uuid4().hex[:16]}"), names.get(name, name),
                              arguments if isinstance(arguments, dict) else raw))
    return tuple(calls)


def api_key(settings: Mapping[str, Any]) -> str:
    """The key from the environment variable `api_key_env` names (.env: kept from models), else `api_key` written
    in the settings themselves (the owner's choice; nothing protects it)."""
    value = os.getenv(str(settings.get("api_key_env") or ""), "").strip().strip("\"'")
    value = value or str(settings.get("api_key") or "").strip()
    if not value or value.lower() in {"disabled", "none", "null"}:
        raise NotConfigured(f"{settings.get('api_key_env') or 'api_key'} is not set")
    return value


def charge(usage) -> float | None:
    """What the provider says this call cost, in dollars, if it says (OpenRouter's `usage.cost`)."""
    value = getattr(usage, "cost", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def cached_tokens(usage) -> int:
    """Prompt tokens read from the provider's cache: OpenAI's `prompt_tokens_details.cached_tokens`, DeepSeek's
    `prompt_cache_hit_tokens` (they bill these far cheaper)."""
    details = getattr(usage, "prompt_tokens_details", None)
    value = getattr(details, "cached_tokens", None) if details is not None else None
    if value is None and isinstance(details, dict):
        value = details.get("cached_tokens")
    if value is None:
        value = getattr(usage, "prompt_cache_hit_tokens", None)
    return int(value or 0)


class OpenAICompatible:
    """OpenAI Chat Completions and compatible APIs (DeepSeek, OpenRouter)."""

    supports_tools = True

    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client
        self._effort_supported = bool(self.settings.get("effort_param", False))
        # Models that take function tools on Chat Completions only without reasoning (OpenAI's GPT-6 Luna: "set
        # reasoning_effort to 'none'"): their tool steps say so, and every other call keeps its effort.
        self._tools_without_reasoning: set[str] = set()

    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key(self.settings), base_url=self.settings.get("base_url") or None,
                                       default_headers=self.settings.get("headers") or None, max_retries=0)
        return self._client

    async def complete(self, model: str, messages, schema, schema_name: str, effort: str,
                       max_tokens: int, timeout_s: float, images: Sequence[Image] = (), tools=()) -> AdapterResult:
        mode = "tools" if tools else self.settings.get("schema_mode", "json_object") if schema else ""
        if schema and not tools and self.settings.get("inline_history"):
            messages = inline_history(messages)
        names = function_names(tools) if tools else {}
        back = {api: name for name, api in names.items()}
        params: dict[str, Any] = {"model": model,
                                  "messages": openai_messages(messages, names) if tools else list(messages),
                                  self.settings.get("token_param", "max_tokens"): max_tokens}
        if tools:  # the model's own function calls (a thinking model refuses a forced choice: DeepSeek 400)
            params["tools"] = [{"type": "function", "function": {
                "name": names[tool.name], "description": tool.description, "parameters": dict(tool.parameters)}}
                for tool in tools]
            params["tool_choice"] = "auto"
        elif schema and mode == "native":
            params["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": bool(self.settings.get("strict_schema", False)), "schema": schema}}
        elif schema and mode == "strict_tool":  # the answer as the arguments of one tool, held to the schema
            params["tools"] = [{"type": "function", "function": {
                "name": schema_name, "strict": True, "parameters": strict_tool_schema(schema),
                "description": "Your answer: call this exactly once, with every field."}}]
            params["tool_choice"] = "auto"  # thinking models refuse a forced choice (DeepSeek: 400)
            params["messages"] = [dict(message) for message in params["messages"]]
            if params["messages"][-1].get("role") == "user":
                params["messages"][-1]["content"] += f"\n\n(Answer by calling {schema_name}.)"
        elif schema:
            params["messages"] = with_schema_instruction(messages, schema)
            params["response_format"] = {"type": "json_object"}
        if tools:  # the last user message, which tool results may follow
            at = next((index for index in range(len(params["messages"]) - 1, -1, -1)
                       if params["messages"][index]["role"] == "user"), None)
        else:
            at = len(params["messages"]) - 1 if params["messages"][-1]["role"] == "user" else None
        if images and at is not None:
            last = dict(params["messages"][at])
            last["content"] = [{"type": "text", "text": last["content"]}, *(
                {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{image.base64}"}}
                for image in images)]
            params["messages"] = [*params["messages"][:at], last, *params["messages"][at + 1:]]
        if self._effort_supported:
            params["reasoning_effort"] = "none" if tools and model in self._tools_without_reasoning else effort
        client = self.client().with_options(timeout=timeout_s, max_retries=0)
        try:
            response = await client.chat.completions.create(**params)
        except Exception as error:
            if "reasoning_effort" not in params or "reasoning_effort" not in str(error):
                raise
            if tools and params["reasoning_effort"] != "none" and "tools" in str(error).lower():
                # This model takes function tools here only without reasoning: remember, for its tool steps alone.
                self._tools_without_reasoning.add(model)
                params["reasoning_effort"] = "none"
            else:
                # Some compatible endpoints reject the parameter; remember and retry once without it.
                self._effort_supported = False
                params.pop("reasoning_effort")
            response = await client.chat.completions.create(**params)
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        cached = cached_tokens(usage)
        charged = charge(usage)
        text = lambda reply: tool_answer(reply.choices[0].message, schema_name, schema) if mode == "strict_tool" \
            else reply.choices[0].message.content or ""
        blank = lambda reply: not text(reply).strip() and not (
            mode == "tools" and openai_calls(reply.choices[0].message, back))
        if mode in ("json_object", "strict_tool", "tools") and blank(response):
            # JSON mode sometimes answers only whitespace (DeepSeek); a blank answer is never usable, so ask once more.
            response = await client.chat.completions.create(**params)
            usage = getattr(response, "usage", None)
            input_tokens += getattr(usage, "prompt_tokens", 0) or 0
            output_tokens += getattr(usage, "completion_tokens", 0) or 0
            cached += cached_tokens(usage)
            charged = None if charged is None or charge(usage) is None else charged + charge(usage)
        choice = response.choices[0]
        reason = str(getattr(choice, "finish_reason", "") or "").lower()
        if mode != "tools":
            return AdapterResult(text(response), input_tokens, output_tokens, reason == "length", mode, cached,
                                 cost=charged)
        reasoning = getattr(choice.message, "reasoning_content", None)  # DeepSeek's thinking: wanted back with tools
        return AdapterResult(text(response), input_tokens, output_tokens, reason == "length", mode, cached,
                             tool_calls=openai_calls(choice.message, back),
                             replay={"reasoning_content": reasoning} if reasoning is not None else None, cost=charged)


    async def list_models(self) -> list[tuple[str, dt.datetime | None]]:
        """(ID, release time): OpenAI-compatible APIs give `created` as a Unix time (some give none)."""
        return [(model.id, dt.datetime.fromtimestamp(model.created, dt.timezone.utc)
                 if isinstance(getattr(model, "created", None), (int, float)) else None)
                async for model in self.client().models.list(timeout=LIST_TIMEOUT_S)]


class Gemini:
    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client

    def client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=api_key(self.settings))
        return self._client

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s,
                       images: Sequence[Image] = ()) -> AdapterResult:
        from google.genai import types
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part(text=m["content"])])
                    for m in messages if m["role"] != "system"]
        if images and contents and contents[-1].role == "user":
            contents[-1].parts.extend(types.Part.from_bytes(data=image.data, mime_type=image.mime) for image in images)
        config: dict[str, Any] = {"max_output_tokens": max_tokens,
                                  "http_options": types.HttpOptions(timeout=int(timeout_s * 1000)),
                                  # The core never passes Python functions; this also silences the SDK's AFC warning.
                                  "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if system:
            config["system_instruction"] = system
        if schema:
            config.update(response_mime_type="application/json", response_json_schema=dict(schema))
        response = await self.client().aio.models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(**config))
        candidate = (response.candidates or [None])[0]
        reason = str(getattr(getattr(candidate, "finish_reason", None), "name", "") or "")
        usage = getattr(response, "usage_metadata", None)
        return AdapterResult(response.text or "", getattr(usage, "prompt_token_count", 0) or 0,
                             getattr(usage, "candidates_token_count", 0) or 0, reason == "MAX_TOKENS",
                             "native" if schema else "",
                             cached_input_tokens=getattr(usage, "cached_content_token_count", 0) or 0)


    async def list_models(self) -> list[str]:
        """Models that can generate text (not embeddings or image-only ones), without the `models/` prefix."""
        from google.genai import types
        pager = await self.client().aio.models.list(config=types.ListModelsConfig(
            http_options=types.HttpOptions(timeout=int(LIST_TIMEOUT_S * 1000))))
        return [model.name.removeprefix("models/") async for model in pager
                if "generateContent" in (model.supported_actions or ())]


UNSUPPORTED_BY_CLAUDE = {"maxItems", "uniqueItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                         "multipleOf", "minLength", "maxLength", "pattern"}


def closed(value: Any) -> bool:
    """Every object in the schema forbids extra properties (Claude's structured outputs require it)."""
    if isinstance(value, Mapping):
        if value.get("type") == "object" and value.get("additionalProperties") is not False:
            return False
        return all(closed(item) for item in value.values())
    if isinstance(value, list):
        return all(closed(item) for item in value)
    return True


def claude_schema(value: Any) -> Any:
    """Claude's structured outputs reject numeric, length and most array limits; drop them there.
    The registry still validates the reply against the full schema, so the limits keep applying."""
    if isinstance(value, Mapping):
        result = {key: claude_schema(item) for key, item in value.items() if key not in UNSUPPORTED_BY_CLAUDE}
        if isinstance(result.get("minItems"), int) and result["minItems"] > 1:
            result.pop("minItems")
        return result
    if isinstance(value, list):
        return [claude_schema(item) for item in value]
    return value


class Anthropic:
    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client

    def client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=api_key(self.settings), max_retries=0)
        return self._client

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s,
                       images: Sequence[Image] = ()) -> AdapterResult:
        native = bool(schema) and closed(schema)
        if schema and not native:
            # Open objects (free-form tool arguments) cannot be enforced natively: ask in the prompt.
            messages = with_schema_instruction(messages, schema)
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]}
        if images and params["messages"] and params["messages"][-1]["role"] == "user":
            last = params["messages"][-1]
            last["content"] = [*({"type": "image", "source": {"type": "base64", "media_type": image.mime,
                                                              "data": image.base64}} for image in images),
                               {"type": "text", "text": last["content"]}]
        if system:
            params["system"] = system
        if self.settings.get("adaptive_thinking") and effort != "none":
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": effort}
            # Thinking consumes output tokens; keep the visible budget intact.
            params["max_tokens"] = max_tokens + int(self.settings.get("thinking_reserve", 2048))
        if native:
            params.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": claude_schema(schema)}
        response = await self.client().with_options(timeout=timeout_s).messages.create(**params)
        text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = getattr(response, "usage", None)
        read = getattr(usage, "cache_read_input_tokens", 0) or 0  # (Anthropic counts cache reads apart from input)
        return AdapterResult(text, (getattr(usage, "input_tokens", 0) or 0) + read,
                             getattr(usage, "output_tokens", 0) or 0,
                             str(getattr(response, "stop_reason", "")) == "max_tokens",
                             "native" if native else "instruction" if schema else "", read)


    async def list_models(self) -> list[tuple[str, dt.datetime | None]]:
        return [(model.id, getattr(model, "created_at", None))
                async for model in self.client().models.list(timeout=LIST_TIMEOUT_S)]


ADAPTERS = {"openai_compatible": OpenAICompatible, "gemini": Gemini, "anthropic": Anthropic}

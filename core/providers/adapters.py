"""Thin provider adapters. Each turns one model call into one API request and reports raw text."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class AdapterResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    schema_mode: str = ""


class NotConfigured(RuntimeError):
    pass


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


def api_key(settings: Mapping[str, Any]) -> str:
    value = os.getenv(str(settings.get("api_key_env") or ""), "").strip().strip("\"'")
    if not value or value.lower() in {"disabled", "none", "null"}:
        raise NotConfigured(f"{settings.get('api_key_env')} is not set")
    return value


class OpenAICompatible:
    """OpenAI Chat Completions and compatible APIs (DeepSeek, OpenRouter)."""

    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client
        self._effort_supported = bool(self.settings.get("effort_param", False))

    def client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=api_key(self.settings), base_url=self.settings.get("base_url") or None,
                                       default_headers=self.settings.get("headers") or None, max_retries=0)
        return self._client

    async def complete(self, model: str, messages, schema, schema_name: str, effort: str,
                       max_tokens: int, timeout_s: float) -> AdapterResult:
        mode = self.settings.get("schema_mode", "json_object") if schema else ""
        params: dict[str, Any] = {"model": model, "messages": list(messages),
                                  self.settings.get("token_param", "max_tokens"): max_tokens}
        if schema and mode == "native":
            params["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": bool(self.settings.get("strict_schema", False)), "schema": schema}}
        elif schema:
            params["messages"] = with_schema_instruction(messages, schema)
            params["response_format"] = {"type": "json_object"}
        if self._effort_supported:
            params["reasoning_effort"] = effort
        client = self.client().with_options(timeout=timeout_s, max_retries=0)
        try:
            response = await client.chat.completions.create(**params)
        except Exception as error:
            if "reasoning_effort" not in params or "reasoning_effort" not in str(error):
                raise
            # Some compatible endpoints reject the parameter; remember and retry once without it.
            self._effort_supported = False
            params.pop("reasoning_effort")
            response = await client.chat.completions.create(**params)
        choice = response.choices[0]
        reason = str(getattr(choice, "finish_reason", "") or "").lower()
        usage = getattr(response, "usage", None)
        return AdapterResult(choice.message.content or "", getattr(usage, "prompt_tokens", 0) or 0,
                             getattr(usage, "completion_tokens", 0) or 0, reason == "length", mode)


class Gemini:
    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client

    def client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=api_key(self.settings))
        return self._client

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s) -> AdapterResult:
        from google.genai import types
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part(text=m["content"])])
                    for m in messages if m["role"] != "system"]
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
                             "native" if schema else "")


class Anthropic:
    def __init__(self, settings: Mapping[str, Any], client: Any = None):
        self.settings = dict(settings)
        self._client = client

    def client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=api_key(self.settings), max_retries=0)
        return self._client

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s) -> AdapterResult:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params: dict[str, Any] = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]}
        if system:
            params["system"] = system
        if self.settings.get("adaptive_thinking"):
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": effort}
            # Thinking consumes output tokens; keep the visible budget intact.
            params["max_tokens"] = max_tokens + int(self.settings.get("thinking_reserve", 2048))
        if schema:
            params.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": dict(schema)}
        response = await self.client().with_options(timeout=timeout_s).messages.create(**params)
        text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = getattr(response, "usage", None)
        return AdapterResult(text, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
                             str(getattr(response, "stop_reason", "")) == "max_tokens", "native" if schema else "")


ADAPTERS = {"openai_compatible": OpenAICompatible, "gemini": Gemini, "anthropic": Anthropic}

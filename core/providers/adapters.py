"""Thin provider adapters, the built-in kinds. Each turns one model call into one API request and reports raw
text. Modules may add kinds of their own (contracts.ModelAdapter, Contributions.adapters)."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts.models import AdapterResult, Image, NotConfigured

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


def api_key(settings: Mapping[str, Any]) -> str:
    """The key from the environment variable `api_key_env` names (.env: kept from models), else `api_key` written
    in the settings themselves (the owner's choice; nothing protects it)."""
    value = os.getenv(str(settings.get("api_key_env") or ""), "").strip().strip("\"'")
    value = value or str(settings.get("api_key") or "").strip()
    if not value or value.lower() in {"disabled", "none", "null"}:
        raise NotConfigured(f"{settings.get('api_key_env') or 'api_key'} is not set")
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
                       max_tokens: int, timeout_s: float, images: Sequence[Image] = ()) -> AdapterResult:
        mode = self.settings.get("schema_mode", "json_object") if schema else ""
        params: dict[str, Any] = {"model": model, "messages": list(messages),
                                  self.settings.get("token_param", "max_tokens"): max_tokens}
        if schema and mode == "native":
            params["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema_name, "strict": bool(self.settings.get("strict_schema", False)), "schema": schema}}
        elif schema:
            params["messages"] = with_schema_instruction(messages, schema)
            params["response_format"] = {"type": "json_object"}
        if images and params["messages"][-1]["role"] == "user":
            last = dict(params["messages"][-1])
            last["content"] = [{"type": "text", "text": last["content"]}, *(
                {"type": "image_url", "image_url": {"url": f"data:{image.mime};base64,{image.base64}"}}
                for image in images)]
            params["messages"] = [*params["messages"][:-1], last]
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
                             "native" if schema else "")


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
        if self.settings.get("adaptive_thinking"):
            params["thinking"] = {"type": "adaptive"}
            params["output_config"] = {"effort": effort}
            # Thinking consumes output tokens; keep the visible budget intact.
            params["max_tokens"] = max_tokens + int(self.settings.get("thinking_reserve", 2048))
        if native:
            params.setdefault("output_config", {})["format"] = {"type": "json_schema", "schema": claude_schema(schema)}
        response = await self.client().with_options(timeout=timeout_s).messages.create(**params)
        text = "\n".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        usage = getattr(response, "usage", None)
        return AdapterResult(text, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
                             str(getattr(response, "stop_reason", "")) == "max_tokens",
                             "native" if native else "instruction" if schema else "")


    async def list_models(self) -> list[tuple[str, dt.datetime | None]]:
        return [(model.id, getattr(model, "created_at", None))
                async for model in self.client().models.list(timeout=LIST_TIMEOUT_S)]


ADAPTERS = {"openai_compatible": OpenAICompatible, "gemini": Gemini, "anthropic": Anthropic}

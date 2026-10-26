"""Model access through the core's Providers block, and the adapter protocol a module may implement."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from contracts.messages import FileRef

Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call (added in 1.7): `parameters` is the JSON schema of its arguments (an object)."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One function call in a model's answer (added in 1.7). `arguments` is the parsed JSON object, checked against
    the tool's `parameters`; `id` pairs the call with its result in later messages."""

    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class ModelRequest:
    """One language-model call.

    `messages` use roles `system`, `user` and `assistant` with plain-text content. With `tools` (1.7), an
    `assistant` message may also carry `tool_calls` (a list of {id, name, arguments}, as the result gave them) and
    `replay` (the result's `replay`, sent back as it is), and a `tool` message carries `tool_call_id` and the
    call's result as `content`.
    `profile` names a configured profile (an ordered list of provider/model candidates);
    `None` uses the session persona's profile, or `default`.
    With `output_schema`, the result's `data` is the parsed, schema-validated JSON object.
    `images` (local JPEG, PNG, GIF or WebP files, at most 6) are shown with the last user message.
    Candidates that cannot see images are tried last and are told images were attached instead.
    `persona` names the session persona the call is for, so her `$model` choice applies and her message
    can say which model wrote it; without it, the core uses the run's only persona with this profile.
    With `tools` (1.7), the model answers in words and calls: the result's `tool_calls` are checked against the
    tools' `parameters`, and `output_schema` is not used. Candidates are tried in order while they can call tools;
    the first that cannot ends the request with `ModelError("tools_unsupported")`. Tool names may hold dots
    (`web.search`); an adapter maps them to what its API allows and back.
    """

    messages: Sequence[Mapping[str, str]]
    profile: str | None = None
    output_schema: Mapping[str, Any] | None = None
    schema_name: str = "output"
    effort: Effort = "low"
    max_tokens: int = 1500
    timeout_s: float = 35.0
    images: Sequence[FileRef] = ()
    persona: str | None = None  # added in 1.3; new fields go last
    tools: Sequence[ToolSpec] = ()  # added in 1.7


@dataclass(frozen=True)
class ModelResult:
    text: str
    data: Any = None
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    schema_mode: str = ""
    # Added in 1.4 (new fields go last): why each candidate tried before this one did not answer, in order
    # ("deepseek/deepseek-flash: blank answer (not JSON)"); empty when the first one did.
    fallbacks: tuple[str, ...] = ()
    # Added in 1.7, for requests with tools: the calls, and what the provider needs back with this answer's
    # assistant message in later requests (`replay` on that message, as it is: the core gives it back only to the
    # provider that wrote it; DeepSeek's thinking travels this way).
    tool_calls: tuple[ToolCall, ...] = ()
    replay: Mapping[str, Any] | None = None


class ModelError(RuntimeError):
    """A model call failed.

    `kind` is one of: `unavailable` (no candidate answered), `invalid_output` (the answer did not
    match the schema or was truncated), `budget_exceeded`, `not_configured`, and (for a request with `tools`)
    `tools_unsupported`: the next candidate in the profile cannot call tools, so the request should be asked again
    in a form without them (the engine's JSON turns), where that candidate and those after it count.
    """

    def __init__(self, kind: str, message: str, raw_text: str = ""):
        super().__init__(message)
        self.kind = kind
        self.raw_text = raw_text


class Models(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResult: ...

    def profiles(self) -> Sequence[str]: ...


# ---- adapters: one provider kind's protocol -------------------------------------------------------------------
@dataclass(frozen=True)
class Image:
    """An image shown with the last user message."""

    mime: str
    data: bytes

    @property
    def base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")


@dataclass(frozen=True)
class AdapterResult:
    """One API call's raw answer. `truncated`: the output hit the token limit. `schema_mode`: how the
    schema was enforced (for example `native`), for the audit log."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    truncated: bool = False
    schema_mode: str = ""
    tool_calls: tuple[ToolCall, ...] = ()  # (1.7) with tools; unparsable arguments stay a string (refused)
    replay: Mapping[str, Any] | None = None  # (1.7) sent back with this assistant message


class NotConfigured(RuntimeError):
    """Raised by an adapter whose provider cannot be used (no key, missing settings): the candidate is skipped."""


class ModelAdapter(Protocol):
    """Talks to one provider's API: one `complete` call is one request, returning the raw text. The core does
    the rest (fallback between candidates, schema validation, spacing, images for models that cannot see).

    `messages` use roles `system`, `user` and `assistant`. With `schema`, ask the API for JSON matching it
    (natively if it can, otherwise in the prompt). `images` go with the last user message. Raise
    NotConfigured when the provider cannot be used; any other exception moves on to the next candidate.
    An adapter may also define `async list_models()`, the models its provider offers: IDs, or (ID, release
    date or datetime) pairs so a provider's `models_since` setting can apply.

    One that can call tools sets `supports_tools = True` and takes `tools` (1.7): it offers them as the API's
    functions, returns the calls in `tool_calls`, and translates `tool_calls`/`replay` on assistant messages and
    `tool` messages into the API's own form. The core passes `tools` only when a request has some."""

    async def complete(self, model: str, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any] | None,
                       schema_name: str, effort: Effort, max_tokens: int, timeout_s: float,
                       images: Sequence[Image] = ()) -> AdapterResult: ...


# Builds the adapter for one provider from its settings (as in core.yml `providers:`), once, on first use.
AdapterFactory = Callable[[Mapping[str, Any]], ModelAdapter]

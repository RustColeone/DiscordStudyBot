"""Model access through the core's Providers block, and the adapter protocol a module may implement."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from contracts.messages import FileRef

Effort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ModelRequest:
    """One language-model call.

    `messages` use roles `system`, `user` and `assistant` with plain-text content.
    `profile` names a configured profile (an ordered list of provider/model candidates);
    `None` uses the session persona's profile, or `default`.
    With `output_schema`, the result's `data` is the parsed, schema-validated JSON object.
    `images` (local JPEG, PNG, GIF or WebP files, at most 6) are shown with the last user message.
    Candidates that cannot see images are tried last and are told images were attached instead.
    """

    messages: Sequence[Mapping[str, str]]
    profile: str | None = None
    output_schema: Mapping[str, Any] | None = None
    schema_name: str = "output"
    effort: Effort = "low"
    max_tokens: int = 1500
    timeout_s: float = 35.0
    images: Sequence[FileRef] = ()


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


class ModelError(RuntimeError):
    """A model call failed.

    `kind` is one of: `unavailable` (no candidate answered), `invalid_output` (the answer did not
    match the schema or was truncated), `budget_exceeded`, `not_configured`.
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


class NotConfigured(RuntimeError):
    """Raised by an adapter whose provider cannot be used (no key, missing settings): the candidate is skipped."""


class ModelAdapter(Protocol):
    """Talks to one provider's API: one `complete` call is one request, returning the raw text. The core does
    the rest (fallback between candidates, schema validation, spacing, images for models that cannot see).

    `messages` use roles `system`, `user` and `assistant`. With `schema`, ask the API for JSON matching it
    (natively if it can, otherwise in the prompt). `images` go with the last user message. Raise
    NotConfigured when the provider cannot be used; any other exception moves on to the next candidate.
    An adapter may also define `async list_models()`, the models its provider offers: IDs, or (ID, release
    date or datetime) pairs so a provider's `models_since` setting can apply."""

    async def complete(self, model: str, messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any] | None,
                       schema_name: str, effort: Effort, max_tokens: int, timeout_s: float,
                       images: Sequence[Image] = ()) -> AdapterResult: ...


# Builds the adapter for one provider from its settings (as in core.yml `providers:`), once, on first use.
AdapterFactory = Callable[[Mapping[str, Any]], ModelAdapter]

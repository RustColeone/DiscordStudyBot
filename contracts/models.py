"""Model access through the core's Providers block."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, Sequence

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

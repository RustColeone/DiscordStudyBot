"""Capabilities: single, well-defined actions the core can run on an engine's or a command's behalf."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from contracts.commands import Command, derive
from contracts.messages import Address, Display, FileRef, Origin, Sender
from contracts.models import Models
from contracts.stores import Store

ResultStatus = Literal["success", "failed", "needs_input", "needs_approval"]
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class Risk(str, Enum):
    """What running the capability can change.

    READ: observes only; may be repeated.
    PREPARE: creates local artifacts (downloads, renders) but changes nothing shared; never repeated within a run.
    WRITE: changes something outside the run (sends, edits, deletes, schedules); requires owner approval unless
    the conversation's assignment pre-approves it; never repeated within a run.
    """

    READ = "read"
    PREPARE = "prepare"
    WRITE = "write"


@dataclass(frozen=True)
class ToolResult:
    """What a capability returns. Capabilities report failures here instead of raising."""

    status: ResultStatus
    summary: str
    files: tuple[FileRef, ...] = ()
    sources: tuple[str, ...] = ()
    data: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False

    def __post_init__(self):
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "sources", tuple(self.sources))

    @classmethod
    def ok(cls, summary: str, **fields) -> "ToolResult":
        return cls("success", summary, **fields)

    @classmethod
    def fail(cls, summary: str, error_code: str = "failed", retryable: bool = False, **fields) -> "ToolResult":
        return cls("failed", summary, error_code=error_code, retryable=retryable, **fields)

    @classmethod
    def ask(cls, question: str, **fields) -> "ToolResult":
        return cls("needs_input", question, **fields)

    def to_dict(self) -> dict:
        return {"status": self.status, "summary": self.summary,
                "files": [file.__dict__ for file in self.files], "sources": list(self.sources),
                "data": dict(self.data), "error_code": self.error_code, "retryable": self.retryable}

    @classmethod
    def from_dict(cls, value: Mapping) -> "ToolResult":
        return cls(value["status"], value["summary"],
                   files=tuple(FileRef(**file) for file in value.get("files", ())),
                   sources=tuple(value.get("sources", ())), data=dict(value.get("data") or {}),
                   error_code=value.get("error_code"), retryable=bool(value.get("retryable")))


class ToolContext(Protocol):
    """Handles the core gives a capability for one call.

    `display` is what the calling conversation can show (for example its upload limit).
    `files` are the files people attached to the run's requests, oldest first.
    `schedule` delivers a message to this conversation once, later; it survives restarts. Interfaces
    receive it as a notice with code `reminder` (`data`: `scheduled_id`, `sender_id`, `sender_name`).
    """

    address: Address
    sender: Sender
    origin: Origin
    run_id: str
    display: Display
    files: tuple[FileRef, ...]
    storage: Store
    models: Models
    workspace: Path

    async def progress(self, text: str) -> None: ...

    async def schedule(self, at: float, text: str) -> str:
        """Deliver `text` to this conversation at epoch seconds `at`. Returns the scheduled ID."""

    def scheduled(self) -> list[Mapping[str, Any]]:
        """This conversation's pending scheduled messages: `id`, `at`, `text`, `sender_id`, `sender_name`."""

    async def unschedule(self, scheduled_id: str) -> bool:
        """Cancel a pending scheduled message of this conversation. False if there is none."""


Handler = Callable[[Mapping[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class Capability:
    """One action.

    `name` is `<extension>.<action>`. `parameters` are JSON-schema properties; `required` lists
    mandatory ones. `needs` lists Display features the conversation must support (for example `files`).
    `internal` capabilities are callable by engines but not offered to models as tools.
    `command`, when set, makes the capability available as `$<command> <text>`, with the text passed
    as `command_argument`. It may be a name or a `Command` declaring options and a template for
    interfaces with native command menus; a name becomes a `Command` with one option for
    `command_argument`.
    """

    name: str
    description: str
    run: Handler
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    risk: Risk = Risk.READ
    internal: bool = False
    owner_only: bool = False
    needs: frozenset[str] = frozenset()
    command: Command | str | None = None
    command_argument: str | None = None
    timeout_s: float = 600.0

    def __post_init__(self):
        if isinstance(self.command, str):
            object.__setattr__(self, "command", derive(self.command, self.description, self.command_argument,
                                                       self.parameters, tuple(self.required)))
        if not NAME_PATTERN.match(self.name):
            raise ValueError(f"Capability name must look like extension.action, got {self.name!r}")
        object.__setattr__(self, "required", tuple(self.required))
        object.__setattr__(self, "needs", frozenset(self.needs))
        object.__setattr__(self, "risk", Risk(self.risk))
        missing = set(self.required) - set(self.parameters)
        if missing:
            raise ValueError(f"{self.name}: required parameters are not declared: {sorted(missing)}")
        if self.command_argument and self.command_argument not in self.parameters:
            raise ValueError(f"{self.name}: command_argument must be a declared parameter")

    @property
    def extension(self) -> str:
        return self.name.split(".", 1)[0]

    def schema(self) -> dict:
        """The description offered to models."""
        return {"name": self.name, "description": self.description, "risk": self.risk.value,
                "parameters": {"type": "object", "properties": dict(self.parameters),
                               "required": list(self.required), "additionalProperties": False}}

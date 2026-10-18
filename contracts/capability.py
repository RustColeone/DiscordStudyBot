"""Capabilities: single, well-defined actions the core can run on an engine's or a command's behalf."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from contracts.commands import Access, Command, derive
from contracts.messages import Address, Display, FileRef, Origin, Sender
from contracts.models import Models
from contracts.stores import Store

ResultStatus = Literal["success", "failed", "needs_input", "needs_approval"]
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class Risk(str, Enum):
    """The capability's tier: what running it can change.

    READ: observes only; may be repeated.
    PREPARE: creates local artifacts (downloads, renders) but changes nothing shared; never repeated within a run.
    WRITE: changes something reversible inside the bot (reminders, notes, routines, music); never repeated
    within a run.
    CRITICAL: affects the machine, other people or permissions (shell, files, services, sending as the owner,
    changing policy); never repeated within a run and never approved by a wildcard.
    Whether a call runs at once or waits for the owner is decided per conversation (see docs/core/ARCHITECTURE.md,
    Permissions); by default reads and prepares run, writes and critical actions ask.
    """

    READ = "read"
    PREPARE = "prepare"
    WRITE = "write"
    CRITICAL = "critical"


TIER_ORDER = (Risk.READ, Risk.PREPARE, Risk.WRITE, Risk.CRITICAL)
SubjectKind = Literal["value", "path"]


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
    `files` are the files people attached to the run's requests, oldest first; `own_files` only those
    the person who started the run attached (in a group, others may add messages to a run).
    `schedule` delivers a message to this conversation once, later; it survives restarts. Interfaces
    receive it as a notice with code `reminder` (`data`: `scheduled_id`, `sender_id`, `sender_name`).
    """

    address: Address
    sender: Sender
    origin: Origin
    run_id: str
    display: Display
    is_direct: bool
    files: tuple[FileRef, ...]
    own_files: tuple[FileRef, ...]
    storage: Store
    models: Models
    workspace: Path
    access: Access  # the caller's effective level: member, admin or creator

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
    `access` is the lowest level that may call it (`member`, `admin`, `creator`; `owner_only` means
    `creator`). The caller's level is `ToolContext.access`, so a capability may also do more for higher
    levels (an admin sees every conversation's items, a member only their own).
    `command`, when set, makes the capability available as `$<command> <text>`, with the text passed
    as `command_argument`. It may be a name or a `Command` declaring options and a template for
    interfaces with native command menus; a name becomes a `Command` with one option for
    `command_argument`.

    `risk` is the tier (see Risk). Attributes refine it; the core enforces them and configuration can
    only make them stricter:
    * `private`: shows the owner's private data (messages, screen, clipboard, files). Offered only in
      conversations private to the owner; its run's output goes nowhere else.
    * `subject`: the parameter that permission rules and saved grants match on (a unit, a chat, a path);
      `subject_kind="path"` also makes the core refuse secret paths (`.env`, keys) to models.
    * `escalate(arguments)`: may return a higher tier for a call (never a lower one).
    * `never_auto`: a model's call always asks the owner; no rule or grant changes that.
    * `grantable`: the owner may answer an approval with "always allow" for that exact subject in that
      conversation (needs `subject`).
    * `confirm`: even the owner's own typed command asks for a one-tap confirmation.
    * `describe(arguments)`: one plain line for approval prompts ("Restart navidrome.service").
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
    access: Access = "member"
    private: bool = False
    subject: str | None = None
    subject_kind: SubjectKind = "value"
    escalate: Callable[[Mapping[str, Any]], "Risk | str | None"] | None = None
    never_auto: bool = False
    grantable: bool = False
    confirm: bool = False
    describe: Callable[[Mapping[str, Any]], str] | None = None

    def __post_init__(self):
        if self.owner_only:
            object.__setattr__(self, "access", "creator")
        if self.access not in ("member", "admin", "creator"):
            raise ValueError(f"{self.name}: access must be member, admin or creator")
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
        if self.subject is not None and self.subject not in self.parameters:
            raise ValueError(f"{self.name}: subject must be a declared parameter")
        if self.subject_kind not in ("value", "path"):
            raise ValueError(f"{self.name}: subject_kind must be value or path")
        if self.grantable and self.subject is None:
            raise ValueError(f"{self.name}: grantable needs a subject")

    @property
    def extension(self) -> str:
        return self.name.split(".", 1)[0]

    def tier(self, arguments: Mapping[str, Any]) -> Risk:
        """The tier of one call: the declared one, raised (never lowered) by `escalate`."""
        if self.escalate is None:
            return self.risk
        try:
            raised = self.escalate(arguments)
        except Exception:
            # Unsure: treat the call as the most sensitive tier rather than run it on a weaker footing.
            return Risk.CRITICAL
        if raised is None:
            return self.risk
        raised = Risk(raised)
        return raised if TIER_ORDER.index(raised) > TIER_ORDER.index(self.risk) else self.risk

    def subject_of(self, arguments: Mapping[str, Any]) -> str | None:
        if self.subject is None or arguments.get(self.subject) is None:
            return None
        return str(arguments[self.subject]).strip()

    def summary(self, arguments: Mapping[str, Any]) -> str:
        """One line for approval prompts."""
        if self.describe is not None:
            try:
                text = str(self.describe(arguments)).strip()
                if text:
                    return text
            except Exception:
                pass
        return f"{self.name} {json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True)}"

    def schema(self) -> dict:
        """The description offered to models."""
        return {"name": self.name, "description": self.description, "risk": self.risk.value,
                "parameters": {"type": "object", "properties": dict(self.parameters),
                               "required": list(self.required), "additionalProperties": False}}

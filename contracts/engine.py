"""Engines: turn loops that decide what to say and which capabilities to use."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping, Protocol, Sequence

from contracts.capability import ToolResult
from contracts.messages import Address, Delivery, Display, FileRef, Origin, Request
from contracts.models import Models
from contracts.stores import Store


class RunYield(str, Enum):
    """How an engine's `run` ends.

    DONE: the request is handled. New inbox messages left unread start a new run.
    WAIT_INPUT: the engine asked a question; the next message in the run's lane resumes this run.
    WAIT_APPROVAL: a tool call returned `needs_approval`; the owner's decision resumes this run.
    """

    DONE = "done"
    WAIT_INPUT = "waiting_input"
    WAIT_APPROVAL = "waiting_approval"


@dataclass(frozen=True)
class EngineSpec:
    name: str
    description: str = ""
    personas: Literal["one", "many"] = "one"


@dataclass(frozen=True)
class Persona:
    """Who speaks: data, not behaviour. Any engine can use any persona."""

    name: str
    display_name: str
    prompt: str
    profile: str = "default"


@dataclass(frozen=True)
class Event:
    """One entry of a run's durable event log.

    kind:
      `input`         a request for this run (`data["request"]` is the Request as a dict)
      `tool_result`   the result of an approved action (`data`: call_key, name, arguments, result);
                      call_key is "call:" + the call_id the engine passed to tools.call
      `approval_denied` the owner denied an action (`data`: call_key, name, arguments)
      `resumed`       the core restarted while the run was active
    """

    index: int
    kind: str
    at: float
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoryItem:
    direction: Literal["in", "out"]
    text: str
    at: float
    sender_name: str = ""
    sender_id: str = ""
    kind: str = "text"
    message_id: str | None = None
    reply_to: str | None = None
    quoted_text: str = ""


@dataclass(frozen=True)
class Activity:
    """Another open run in the same conversation.

    status: `working` (waiting on a capability or a file delivery, e.g. a video conversion), `running`
    (thinking), `waiting_input` or `waiting_approval`. `doing` names the capability while working.
    """

    run_id: str
    status: str
    sender_id: str
    sender_name: str
    request: str
    doing: str = ""
    since: float = 0.0
    started_at: float = 0.0


class Tools(Protocol):
    def list(self) -> Sequence[Mapping[str, Any]]:
        """Capability schemas this session may offer to a model."""

    async def call(self, name: str, arguments: Mapping[str, Any], call_id: str | None = None) -> ToolResult:
        """Run a capability under policy. Never raises for policy decisions; see ToolResult.status.

        `call_id` identifies the call within the run. Repeating a completed call_id returns the stored
        result instead of running again, which makes engines safe to replay after a restart.
        """


class History(Protocol):
    def recent(self, limit: int = 30) -> Sequence[HistoryItem]: ...


DIARY_KINDS = ("fact", "episode", "reflection")


@dataclass(frozen=True)
class DiaryNote:
    """One note a persona keeps about one person in one conversation. Times are Unix seconds."""

    id: int
    key: str
    kind: str
    note: str
    importance: int
    created_at: float
    updated_at: float


class Diary(Protocol):
    """One persona's notes about the person who sent the run's trigger, in this conversation only.

    People can read and delete what is kept about them with `$memory`. Routine runs have no person,
    so their diary is empty and refuses writes.
    """

    persona: str
    available: bool

    def recall(self, text: str = "", limit: int = 8) -> list[DiaryNote]:
        """Notes matching words in `text` first, then the most important and recent ones."""

    def write(self, key: str, kind: str, note: str, importance: int) -> bool:
        """Create or replace the note under `key`. Raises ValueError for an invalid note, one that copies
        the message instead of paraphrasing it, or past the per-run limit. Returns False when unchanged."""


class Session(Protocol):
    """Everything an engine may use for one run. All access goes through these handles."""

    run_id: str
    address: Address
    origin: Origin
    display: Display
    personas: Sequence[Persona]
    trigger: Request
    events: Sequence[Event]
    state: dict
    resumed: bool
    models: Models
    tools: Tools
    history: History
    options: Mapping[str, Any]

    def memory(self, persona: str) -> Store:
        """The persona's private key-value store, shared by all its conversations."""

    def diary(self, persona: str) -> Diary:
        """The persona's notes about the trigger's sender in this conversation."""

    def inbox(self) -> list[Request]:
        """Requests for this run's lane that arrived while this run was thinking. Each is returned once.
        Requests arriving while the run waits on a capability or file delivery start their own run."""

    def activity(self) -> list[Activity]:
        """Other open runs in this conversation (work already in progress)."""

    async def save(self, state: dict) -> None:
        """Persist engine state (JSON). Call after each meaningful step so a restart can resume."""

    async def reply(self, text: str, quote: str | None = None, persona: str | None = None,
                    label: str = "") -> Delivery:
        """Say `text` in the conversation. `persona` (one of the session's) marks who speaks, so the
        interface can name them and history records them; `label` qualifies it, e.g. a work note."""

    async def send_file(self, file: FileRef, caption: str = "") -> Delivery: ...

    async def progress(self, text: str) -> None: ...


class Engine(ABC):
    spec: EngineSpec

    @abstractmethod
    async def run(self, session: Session) -> RunYield:
        """Handle the trigger (and any resumed events). Must be safe to call again after a restart."""

"""Engines: turn loops that decide what to say and which capabilities to use."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, Union

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


# The message a run's task is cancelled with when the core stops (added in 1.7): the run stays open and is replayed
# on the next start, so an engine that keeps state elsewhere must not record it as cancelled. Any other cancellation
# (`$cancel`, a conversation cancelled) is final.
SHUTDOWN = "core-shutdown"


@dataclass(frozen=True)
class EngineSpec:
    name: str
    description: str = ""
    personas: Literal["one", "many"] = "one"


PART_PLACES = ("stable", "turn")


@dataclass(frozen=True)
class PartContext:
    """What a computed prompt part may look at: the persona it is written for, and the run's address and trigger."""

    persona: "Persona"
    address: Address
    trigger: Request
    owner: bool = False


@dataclass(frozen=True)
class PromptPart:
    """A piece of a persona's prompt that engines place for it: its personality, house rules, memory, anything.

    `text` is the text, or a function of a `PartContext` returning it ("" adds nothing). `place` says where an
    engine puts it: `stable` in the system prompt (the same every turn, so the provider's prompt cache keeps
    serving it), `turn` in the turn's context after the history (it may change every turn). Parts come in `order`
    (lower first). A module's part is for the personas it names (`personas`), or for every persona when it names
    none; a persona's own parts are its own. Engines get them rendered with `Session.prompt`."""

    name: str
    text: Union[str, Callable[[PartContext], str]]
    place: str = "stable"
    order: int = 100
    personas: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.name or not isinstance(self.name, str):
            raise ValueError("a prompt part needs a name")
        if self.place not in PART_PLACES:
            raise ValueError(f"prompt part {self.name!r}: place is one of {', '.join(PART_PLACES)}")
        if not isinstance(self.text, str) and not callable(self.text):
            raise ValueError(f"prompt part {self.name!r}: text is a string or a function of a PartContext")
        if isinstance(self.order, bool) or not isinstance(self.order, int):
            raise ValueError(f"prompt part {self.name!r}: order is a whole number")
        personas = (self.personas,) if isinstance(self.personas, str) else self.personas
        if not isinstance(personas, (tuple, list)) or not all(isinstance(name, str) and name for name in personas):
            raise ValueError(f"prompt part {self.name!r}: personas is a list of persona names")
        object.__setattr__(self, "personas", tuple(personas))


@dataclass(frozen=True)
class Persona:
    """Who speaks: data, not behaviour. Any engine can use any persona. `parts` are prompt parts of its own (its
    personality, say), which engines place beside `prompt` (see PromptPart)."""

    name: str
    display_name: str
    prompt: str
    profile: str = "default"
    parts: tuple[PromptPart, ...] = ()  # added later: new fields go last

    def __post_init__(self):
        if not isinstance(self.parts, (tuple, list)) or not all(isinstance(part, PromptPart) for part in self.parts):
            raise ValueError(f"persona {self.name!r}: parts is a list of PromptPart")
        names = [part.name for part in self.parts]
        if len(set(names)) != len(names):
            raise ValueError(f"persona {self.name!r}: two parts have the same name")
        object.__setattr__(self, "parts", tuple(self.parts))


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
    files: tuple[FileRef, ...] = ()  # attachments (for example an image sent before the question)


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


@dataclass(frozen=True)
class HistorySummary:
    """A conversation's rolling summary: `text` covers its messages up to `upto` (epoch seconds, inclusive)."""

    text: str
    upto: float
    updated_at: float


class History(Protocol):
    """The conversation's history, kept by the core (every message in and out)."""

    def recent(self, limit: int = 30) -> Sequence[HistoryItem]: ...

    def search(self, text: str, limit: int = 20) -> Sequence[HistoryItem]:
        """Messages of this conversation containing `text` (any case, however a letter is written), newest first,
        beyond `recent`'s reach."""

    def summary(self) -> HistorySummary | None:
        """The conversation's rolling summary, if an engine wrote one."""

    def summarize(self, text: str, upto: float) -> None:
        """Replace the rolling summary: `text` (1 to 8000 characters) covers messages up to `upto`, a finite time;
        anything else raises ValueError. Engines compact older history this way; the summary is shared by every engine and persona in the
        conversation, and people can read and clear it with `$memory`."""


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
    owner: bool  # the trigger's sender is an owner of its interface (never for routines)
    access: str  # the trigger's sender's effective level: member, admin or creator

    def store(self, *, conversation: bool = False, persona: str | None = None, person: bool = False) -> Store:
        """A key-value store of this engine's module (added in 1.5), scoped by any combination of: this
        `conversation`, one of the session's personas (across conversations), and the trigger's sender
        (`person`, across conversations). No arguments: the module's store everywhere. Each module's stores
        are its own; the core keeps them and never reads them."""

    def memory(self, persona: str) -> Store:
        """The persona's private key-value store, shared by all its conversations (older: prefer
        `store(persona=...)`, which is the module's own)."""

    def diary(self, persona: str) -> Diary:
        """The persona's notes about the trigger's sender. Notes are filed in the conversation they were written
        in; with the assignment's `diary_scope` option, those of every conversation (same interface) sharing
        that name are read and corrected together."""

    def prompt(self, persona: str, place: str = "stable") -> str:
        """The prompt parts for one of the run's personas at `place` ("stable" or "turn"), rendered in order and
        joined ("" when there are none): its own parts and the modules' parts for it. A part that fails to render
        is left out (and logged); none can fail the run."""
        ...

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

    async def stopped(self, session: Session, reason: str) -> None:
        """A run of yours that was waiting (for input or an approval) was cancelled. Nothing of yours was running,
        so no CancelledError reached `run`: put right what still shows the work as going (`session.state` is the
        run's last saved state; it does not resume). Optional; by default nothing happens."""

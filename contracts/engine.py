"""Engines: turn loops that decide what to say and which capabilities to use."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence, Union

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
    """`formerly` (added in 1.11): engine names it had before. An assignment naming one (in core.yml, set at runtime
    or suggested by a module) is read as this engine, and its open runs carry on in it (with the state the old engine
    saved: an engine that cannot read it ends the run)."""

    name: str
    description: str = ""
    personas: Literal["one", "many"] = "one"
    formerly: tuple[str, ...] = ()

    def __post_init__(self):
        formerly = (self.formerly,) if isinstance(self.formerly, str) else self.formerly
        if not isinstance(formerly, (tuple, list)) or not all(isinstance(item, str) and item.strip()
                                                               for item in formerly):
            raise ValueError(f"engine {self.name!r}: formerly is a list of names")
        object.__setattr__(self, "formerly", tuple(item.strip() for item in formerly))
        if self.name in self.formerly:
            raise ValueError(f"engine {self.name!r}: formerly lists its own name")


PART_PLACES = ("stable", "turn")


@dataclass(frozen=True)
class PartContext:
    """What a computed prompt part may look at: the persona it is written for, the run's address and trigger, and
    (added in 1.9) the engine placing it, so a part can keep to some engines."""

    persona: "Persona"
    address: Address
    trigger: Request
    owner: bool = False
    engine: str = ""


@dataclass(frozen=True)
class PromptPart:
    """A piece of a persona's prompt that engines place for it: its personality, house rules, memory, anything.

    `text` is the text, or a function of a `PartContext` returning it ("" adds nothing); since 1.14 the function may
    be async (a part that looks something up), and engines render it with `Session.aprompt` (`Session.prompt` leaves
    an async part out). `place` says where an
    engine puts it: `stable` in the system prompt (the same every turn, so the provider's prompt cache keeps
    serving it), `turn` in the turn's context after the history (it may change every turn). Parts come in `order`
    (lower first). A module's part is for the personas it names (`personas`), or for every persona when it names
    none; a persona's own parts are its own. Engines get them rendered with `Session.prompt`."""

    name: str
    text: Union[str, Callable[[PartContext], Union[str, Awaitable[str]]]]
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


# The model tiers a persona may name besides her `profile` (Persona.profiles, added in 1.11).
TIERS = ("own", "strong")


@dataclass(frozen=True)
class Persona:
    """Who speaks: data, not behaviour. A persona with her model profile is an agent; any engine can use any of them.
    `parts` are prompt parts of its own (its personality, say), which engines place beside `prompt` (see PromptPart).

    Added in 1.9: `aliases`, other names people call her by ("Luna, …" still calls on GPT); `formerly`, persona
    names she had before, so the core moves what it keeps under them to her name and reads core.yml's old name as
    hers (modules that keep their own data by persona name do the same); `pictures`, kind → image file (`avatar`,
    square; `card`, 3:4; any other kind a page knows), which interfaces show from `InterfacePort.personas`.

    Added in 1.11: `description`, one line on who she is, for engines that write their own prompts around her rather
    than placing `prompt` (Chimera's work prompts); `profiles`, her model profiles by tier besides `profile`: `own`
    (her own models only, with nobody else's as a fallback, so the model family answering is hers) and `strong` (her
    strongest models). An engine asking for a tier she has not named uses `own`, then `profile`.

    Added in 1.13: `allowance`, her own default daily allowance in dollars (0: no daily limit; None: the core's
    default). The owner's settings come first: core.yml `stamina:` (`personas`, then `allowance`) and `$stamina`.

    Added in 1.15: `effort`, how hard she thinks in a conversation (an `Effort`; empty: the engine's setting). A
    session's own `effort` comes first; "none" answers at once, without a thinking phase."""

    name: str
    display_name: str
    prompt: str
    profile: str = "default"
    parts: tuple[PromptPart, ...] = ()  # added later: new fields go last
    aliases: tuple[str, ...] = ()
    formerly: tuple[str, ...] = ()
    pictures: Mapping[str, str] = field(default_factory=dict)
    description: str = ""
    profiles: Mapping[str, str] = field(default_factory=dict)
    allowance: float | None = None
    effort: str = ""

    def tier(self, name: str) -> str:
        """The profile for tier `name` (`own`, `strong`): hers, else `own`, else `profile`."""
        return self.profiles.get(name) or self.profiles.get("own") or self.profile

    def __post_init__(self):
        if not isinstance(self.parts, (tuple, list)) or not all(isinstance(part, PromptPart) for part in self.parts):
            raise ValueError(f"persona {self.name!r}: parts is a list of PromptPart")
        names = [part.name for part in self.parts]
        if len(set(names)) != len(names):
            raise ValueError(f"persona {self.name!r}: two parts have the same name")
        object.__setattr__(self, "parts", tuple(self.parts))
        for field_name in ("aliases", "formerly"):
            value = getattr(self, field_name)
            value = (value,) if isinstance(value, str) else value
            if not isinstance(value, (tuple, list)) or not all(isinstance(item, str) and item.strip()
                                                                 for item in value):
                raise ValueError(f"persona {self.name!r}: {field_name} is a list of names")
            object.__setattr__(self, field_name, tuple(item.strip() for item in value))
        if self.name in self.formerly:
            raise ValueError(f"persona {self.name!r}: formerly lists her own name")
        if not isinstance(self.pictures, Mapping) or not all(isinstance(kind, str) and kind and isinstance(path, str)
                                                             and path for kind, path in self.pictures.items()):
            raise ValueError(f"persona {self.name!r}: pictures maps a kind (avatar, card, …) to an image file")
        object.__setattr__(self, "pictures", dict(self.pictures))
        if not isinstance(self.description, str):
            raise ValueError(f"persona {self.name!r}: description is one line of text")
        object.__setattr__(self, "description", " ".join(self.description.split()))
        if not isinstance(self.profiles, Mapping) or not all(
                tier in TIERS and isinstance(profile, str) and profile for tier, profile in self.profiles.items()):
            raise ValueError(f"persona {self.name!r}: profiles maps a tier ({', '.join(TIERS)}) to a profile name")
        object.__setattr__(self, "profiles", dict(self.profiles))
        if self.allowance is not None:
            if isinstance(self.allowance, bool) or not isinstance(self.allowance, (int, float)) or self.allowance < 0:
                raise ValueError(f"persona {self.name!r}: allowance is dollars a day (0 or more)")
            object.__setattr__(self, "allowance", float(self.allowance))


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
    def list(self, persona: str = "") -> Sequence[Mapping[str, Any]]:
        """Capability schemas this session may offer to a model; with `persona` (added in 1.14), to that persona's
        turn (a capability for some personas only is left out of the others')."""

    async def call(self, name: str, arguments: Mapping[str, Any], call_id: str | None = None, *,
                   persona: str = "") -> ToolResult:
        """Run a capability under policy. Never raises for policy decisions; see ToolResult.status.

        `call_id` identifies the call within the run. Repeating a completed call_id returns the stored
        result instead of running again, which makes engines safe to replay after a restart. `persona` (added in
        1.14) is whose turn made the call: the capability sees it as `ToolContext.persona`. Without it, a run of
        one persona is hers, and a run of several is nobody's in particular.
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


@dataclass(frozen=True)
class LogEntry:
    """One line of the log: something that happened in a conversation. `module` wrote it ("core" for what the core
    logs itself), `kind` is one short word (`note`, `action`, `landed`, …), `subject` the person it is about ("": nobody
    in particular), `persona` the one it concerns, if any. Times are Unix seconds."""

    id: int
    at: float
    module: str
    kind: str
    text: str
    address: str
    subject: str = ""
    persona: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)


class Logbook(Protocol):
    """A conversation's log (added in 1.15, in place of the diary, which only kept what a persona noted of someone):
    what happened here, written by modules through the handles they already have, read by engines, and by people with
    `$log`. The core logs by itself every write and critical action that ran. A model never writes here directly, only
    through a capability a module offers. It is not the audit log, the core's own record for the owner, which models
    never read."""

    def write(self, kind: str, text: str, *, person: bool = False, persona: str = "",
              data: Mapping[str, Any] | None = None) -> LogEntry:
        """Add one line (`person`: about the person this run or call is for). Raises ValueError for an empty text or
        a kind that is not one short word."""

    def recent(self, limit: int = 8, kind: str = "") -> list[LogEntry]:
        """The newest lines of this conversation, oldest of them first (`kind`: of that kind only)."""

    def find(self, text: str, limit: int = 8) -> list[LogEntry]:
        """The newest lines of this conversation holding words of `text`."""


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
    logbook: Logbook  # this conversation's log (added in 1.15)

    def store(self, *, conversation: bool = False, persona: str | None = None, person: bool = False) -> Store:
        """A key-value store of this engine's module (added in 1.5), scoped by any combination of: this
        `conversation`, one of the session's personas (across conversations), and the trigger's sender
        (`person`, across conversations). No arguments: the module's store everywhere. Each module's stores
        are its own; the core keeps them and never reads them."""

    def memory(self, persona: str) -> Store:
        """The persona's private key-value store, shared by all its conversations (older: prefer
        `store(persona=...)`, which is the module's own)."""

    def prompt(self, persona: str, place: str = "stable") -> str:
        """The prompt parts for one of the run's personas at `place` ("stable" or "turn"), rendered in order and
        joined ("" when there are none): its own parts and the modules' parts for it. A part that fails to render
        is left out (and logged); none can fail the run. An async part is left out too: use `aprompt`."""
        ...

    async def aprompt(self, persona: str, place: str = "stable") -> str:
        """`prompt`, with async parts awaited (added in 1.14): each in turn, for at most a few seconds (one that takes
        longer is left out, and logged). Render once per turn: a part may cost a lookup."""
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

"""Platform-neutral messages exchanged between interfaces and the core."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal, Mapping

Origin = Literal["person", "routine", "system"]
SenderKind = Literal["person", "bot", "system"]
ReplyKind = Literal["text", "file", "progress", "notice", "event"]
ReplySource = Literal["run", "command", "owner", "core"]
MessageKind = Literal["text", "image", "video", "voice", "file", "sticker", "other"]


def _frozen_mapping(value) -> Mapping:
    # A private copy: callers cannot change the request through the dict they passed in.
    return dict(value or {})


@dataclass(frozen=True)
class Address:
    """One conversation: an interface name plus that interface's own conversation ID."""

    interface: str
    conversation: str

    def __post_init__(self):
        if not self.interface or ":" in self.interface:
            raise ValueError("Address.interface must be a nonempty name without ':'")
        if not self.conversation:
            raise ValueError("Address.conversation must be nonempty")

    def __str__(self) -> str:
        return f"{self.interface}:{self.conversation}"

    @classmethod
    def parse(cls, text: str) -> "Address":
        interface, separator, conversation = str(text).partition(":")
        if not separator:
            raise ValueError(f"Address must look like interface:conversation, got {text!r}")
        return cls(interface, conversation)


@dataclass(frozen=True)
class Sender:
    """Who sent a request. `id` is stable within the interface (a platform user ID)."""

    id: str
    name: str = ""
    kind: SenderKind = "person"


@dataclass(frozen=True)
class FileRef:
    """A local file the core and interfaces can read."""

    path: str
    name: str = ""
    mime: str = ""
    size: int = 0


@dataclass(frozen=True)
class Display:
    """What a conversation can show. Engines adapt their output; interfaces render the rest."""

    markdown: bool = False
    edits: bool = False
    progress: bool = True
    quotes: bool = False
    files: bool = True
    max_upload_mb: float = 8.0
    max_text_chars: int = 2000

    def features(self) -> frozenset[str]:
        return frozenset(name for name in ("markdown", "edits", "progress", "quotes", "files")
                         if getattr(self, name))


@dataclass(frozen=True)
class Request:
    """One inbound message or trigger.

    `id` must be unique within the interface; the gateway drops a second request with the same ID.
    Facts such as `is_direct` and `mentions_me` are reported by the interface; the gateway's
    admission rules decide what they mean.
    """

    id: str
    address: Address
    sender: Sender
    text: str
    received_at: float
    origin: Origin = "person"
    is_direct: bool = True
    mentions_me: bool | None = None
    reply_to: str | None = None
    quoted_text: str = ""
    kind: MessageKind = "text"
    files: tuple[FileRef, ...] = ()
    display: Display = field(default_factory=Display)
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            raise ValueError("Request.id must be nonempty")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "extra", _frozen_mapping(self.extra))


@dataclass(frozen=True)
class Action:
    """A button: `label` shown, `command` (`$approve ea947b`) run as whoever presses it."""

    label: str
    command: str
    style: Literal["primary", "secondary", "success", "danger"] = "secondary"


@dataclass(frozen=True)
class Reply:
    """One outbound item. Interfaces render it in their platform's form.

    `kind="notice"` carries a machine-readable `code` (for example `run_failed`,
    `approval_request`) so each interface can phrase it appropriately. `kind="event"` is an
    interface notification for the owner (see InterfacePort.notify).
    `source` says who produced it: `run` (an engine), `command`, `owner` (an owner's relay or send),
    or `core`. `speaker` is the display name of the persona talking, set by engines with several
    personas; `label` qualifies it (for example a work note). Interfaces show both in their own way.
    `actions` are buttons, each running a `$command` as the person who presses it (interfaces without
    buttons show the commands as text). `private` asks to show the reply only to the person it answers,
    where the platform can (otherwise it is sent normally). `run_id` names the run a reply is about: its
    output and progress (progress carries a `Cancel` action), or the `$cancel` confirmation for it, after
    which interfaces can clear that run's progress display.
    """

    address: Address
    kind: ReplyKind
    text: str = ""
    file: FileRef | None = None
    quote: str | None = None
    in_reply_to: str | None = None
    run_id: str | None = None
    code: str | None = None
    data: Mapping[str, str] = field(default_factory=dict)
    source: ReplySource = "core"
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    speaker: str = ""
    label: str = ""
    actions: tuple["Action", ...] = ()
    private: bool = False

    def __post_init__(self):
        if self.kind == "file" and self.file is None:
            raise ValueError("A file reply needs a file")
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "data", _frozen_mapping(self.data))


@dataclass(frozen=True)
class Delivery:
    """The interface's result for one reply. `uncertain=True` means it may have been sent; never retry it."""

    ok: bool
    message_id: str | None = None
    detail: str = ""
    uncertain: bool = False


@dataclass(frozen=True)
class SubmitResult:
    """Outcome of InterfacePort.submit. Truthy when the request was durably accepted.

    `reason`: `accepted`, `relayed` (an owner reply forwarded elsewhere), `command`, `duplicate`, or the
    admission decision (`disabled`, `not_mentioned`, `sender_not_allowed`, `owner_only`,
    `before_rule_change`, `too_old`).
    """

    accepted: bool
    reason: str

    def __bool__(self) -> bool:
        return self.accepted

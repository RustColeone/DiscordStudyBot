"""Interfaces: connect a platform to the gateway."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence

from contracts.capability import Risk, ToolContext, ToolResult
from contracts.commands import Command
from contracts.messages import Address, Delivery, Display, FileRef, Reply, Request, Sender, SubmitResult
from contracts.stores import Store


@dataclass(frozen=True)
class Operation:
    """A platform-specific action (for example `login`, or playing music) run through the core.

    It becomes the capability `<interface>.<name>`; `command`, if set, also makes it available as
    `$<command> <text>` with the text passed as `command_argument`. Operations are owner-only unless
    `owner_only=False`; a public operation must act only on the calling conversation
    (`context.address`), never on others. `risk` and the attributes (`private`, `subject`, `subject_kind`,
    `never_auto`, `grantable`, `confirm`) mean the same as on a Capability.
    """

    name: str
    description: str
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    risk: Risk = Risk.READ
    command: Command | str | None = None
    command_argument: str | None = None
    timeout_s: float = 120.0
    owner_only: bool = True
    private: bool = False
    subject: str | None = None
    subject_kind: str = "value"
    never_auto: bool = False
    grantable: bool = False
    confirm: bool = False


class InterfacePort(Protocol):
    """What the gateway gives an interface. Every method only acts on this interface's own addresses."""

    name: str
    bottle: Store

    async def submit(self, request: Request) -> SubmitResult:
        """Hand one request to the core. Returns after it is durably accepted (truthy) or rejected by
        admission or deduplication (falsy, with a reason). Advance any read cursor only after this returns."""

    async def notify(self, text: str, *, origin: Address | None = None, code: str = "event",
                     data: Mapping[str, str] | None = None, files: Sequence[FileRef] = ()) -> Delivery:
        """Tell the owner something (an incoming message, a login change). Delivered as a Reply of kind
        `event` to the route configured for `origin` (kinds containing "event"), else the control route.
        An owner's reply to that delivered event is relayed back to `origin` as an owner message."""

    async def observe(self, address: Address, text: str, *, direction: Literal["in", "out"] = "in",
                      sender: Sender | None = None, kind: str = "text", message_id: str | None = None,
                      at: float | None = None, reply_to: str | None = None, quoted_text: str = "",
                      files: tuple[FileRef, ...] = ()) -> None:
        """Record activity the core should know about but not act on (for example a reply someone typed
        on the phone, group chatter not addressed to the bot, an image sent before the question, or a late
        receipt) in conversation history. `files` are local copies engines may show to models.
        Idempotent per (address, direction, message_id)."""

    async def cancel(self, address: Address, reason: str) -> int:
        """Cancel open runs of a conversation because the platform state changed (a manual reply).
        Returns how many runs were cancelled."""

    async def cancel_all(self, reason: str) -> int:
        """Cancel open runs of every conversation of this interface (an account switch or sign-out)."""

    def check(self, reply: Reply) -> str | None:
        """Re-check a reply right before committing it (after waiting for spacing or a lock).
        Returns None if it may still be sent, else a reason (`run_closed`, `conversation_disabled`,
        `not_admitted`). Only run output can become invalid; other replies always return None."""

    def policy(self, address: Address) -> Mapping[str, Any]:
        """Read-only view of the core's current policy for one of your conversations:
        `{"admission": {...rule...}, "assignment": {...} | None}`."""

    def set_ready(self, ready: bool, detail: str = "") -> None:
        """Report whether the platform can send. While not ready, deliveries are refused (not uncertain)
        and the owner is told about each transition."""

    def set_owners(self, sender_ids: Sequence[str], control: Address | None = None, *,
                   fallback: bool = False) -> None:
        """Report the platform's own notion of the owner (for example a bot application's owner).
        Used only when core.yml lists no owners for this interface; `control` (one of your addresses,
        such as the owner's direct conversation) is used only when core.yml sets no control route.
        `fallback=True` offers `control` only for when no other interface offers one (a local console
        receiving approvals while nothing else is connected)."""

    def commands(self) -> list[Command]:
        """Every `$command` people can run here (built-in, capability and operation commands), for
        interfaces that offer a native command menu or their own help. Access is still checked per
        request; `Command.access` says the lowest level that may run each one."""

    def models_ready(self, profile: str) -> bool:
        """Whether some candidate of the model profile can be called (its provider has a key), for showing who
        can answer before anyone asks (added in 1.7; engines and capabilities have `Models.ready`)."""

    def publish(self, *, link: str | None = None, status: Mapping[str, Any] | None = None) -> None:
        """Tell the other interfaces where people reach this one (`link`, a URL) and a small public status (JSON,
        at most 16 KB), so they can link to it and show what happens there (added in 1.7). Each call replaces
        what it names; `status` may describe a place, see PLACE_STATUS. Kept while the core runs."""

    def directory(self) -> Mapping[str, Mapping[str, Any]]:
        """What every interface has published: name → `{"link", "status", "ready"}` (added in 1.7)."""

    def approval_status(self, approval_id: str) -> str | None:
        """`pending`, `approved`, `denied` or `expired` for an approval asked in one of this interface's
        conversations, else None (added in 1.7). An interface that shows approval cards asks this, so a card decided
        elsewhere (typed, in another interface) or expired with its run stops offering its buttons."""

    def personas(self) -> Sequence[Mapping[str, Any]]:
        """Every persona the core has, in the order modules gave them (added in 1.9): `name`, `display_name`,
        `aliases`, `formerly`, `pictures` (kind → absolute image path, see Persona) and `ready` (some candidate of
        her profile can be called). Pages show the students from this rather than keeping their own list."""

    def prices(self) -> Any:
        """Added in 1.12: the core's model prices (contracts.Prices), to show them on a page."""

    def spending(self, days: int = 14) -> Sequence[Mapping[str, Any]]:
        """Added in 1.12: what was spent each day (everything: chats, sessions, memories), newest last, for the last
        `days` days: `[{day, total, personas: {name: dollars}}]`."""

    def stamina(self, persona: str | None = None) -> Mapping[str, Any] | None:
        """Added in 1.12: what a persona has spent today against her daily allowance, and the account she spends from:
        `{today: {spent, allowance, left, day}, account: {provider, kind (balance or budget), currency, total, left,
        as_of}}` (allowance and left None without a daily limit; account None when none is set up). None for one the
        core does not know. With no persona: everyone's day against the day's total, `{today: {...}}`. A run that
        reaches her allowance, the total, or its own `budget_usd` waits for the owner."""

    def facts(self, persona: str, sender_id: str | None = None) -> Mapping[str, Any]:
        """What modules publish about a persona (`<module>.<key>` → value, see contracts.Facts), and with
        `sender_id`, what they publish about her and that person of this interface over it (added in 1.9)."""

    def log(self, event: str, **fields: Any) -> None: ...


# The shape of a published `status` that describes a place people can go (a classroom, a lab), so any interface
# can draw any other place: {"place": {"name", "icon", "note"}, "people": [{"key", "name", "avatar", "doing",
# "title"}], "away": [keys of people from elsewhere who are here now], "tickets": [{"key", "by", "title", "link"}:
# invitations to come here, held by people elsewhere], "views": [{"name", "icon", "path", "order", "off", "note"}: the
# tabs this place adds to every page's places bar, paths relative to its link, sorted by `order` across places; `off`
# greys one, `note` says why], "you": {"path", "name"}: your tab in the header}. A key is the name lowercased, with only its letters and digits
# kept, in any script ("Flash-chan" → "flashchan", "露娜" → "露娜"); an empty key matches nobody.
PLACE_STATUS = ("place", "people", "away", "tickets", "views", "you")


class Interface(ABC):
    """One platform connection. Only interfaces know platform details."""

    name: str
    display: Display = Display()
    operations: tuple[Operation, ...] = ()

    @abstractmethod
    async def start(self, port: InterfacePort) -> None:
        """Connect and begin submitting requests. Must return promptly; run loops as tasks."""

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def deliver(self, reply: Reply) -> Delivery:
        """Render and send one reply. May take minutes for uploads; must not retry an uncertain send."""

    async def operate(self, operation: str, arguments: Mapping[str, Any], context: ToolContext) -> ToolResult:
        """Run an admin operation. `context` describes the caller (address, sender, run) and offers
        `workspace` and `progress` like any capability call."""
        return ToolResult.fail(f"{self.name} has no operation {operation!r}", "unknown_operation")

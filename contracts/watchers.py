"""Watchers: modules that learn from what personas say, whichever engine they spoke in (added in 1.9)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from contracts.engine import History, Logbook, Persona
from contracts.messages import Address, Request
from contracts.models import Models
from contracts.stores import Facts, Store

NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Exchange:
    """What a watcher sees: one message a persona said (`text`), once it was delivered and recorded, and the
    message she answered (`trigger`, the run's). A run in which she speaks three times gives three exchanges.
    Progress lines, work notes (a reply with a label) and command output give none."""

    address: Address
    persona: Persona
    text: str
    trigger: Request
    run_id: str
    engine: str
    at: float
    owner: bool = False  # the trigger's sender is an owner of its interface (as Session.owner)
    personas: tuple[str, ...] = ()  # every persona taking part in the conversation (the run's), her own included

    @property
    def person(self) -> tuple[str, str]:
        """Who she answered, as `Facts` and interfaces name a person: (interface, sender id)."""
        return (self.address.interface, self.trigger.sender.id)


class WatchContext(Protocol):
    """What a watcher may use for one exchange.

    `models` calls models with the persona's profile unless a request names another; the calls are counted in the
    run's usage (`watcher_calls`, tokens) but not against its budget, since the run may be over. `history` reads
    the conversation (what others said around her message); `store` is the module's own, scoped as
    `Session.store`, with the exchange's person and persona; `facts` publishes for interfaces; `logbook` (added in
    1.15) is the conversation's log, as `Session.logbook`."""

    models: Models
    history: History
    facts: Facts
    logbook: Logbook

    def store(self, *, conversation: bool = False, persona: bool = False, person: bool = False) -> Store: ...

    def log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class Watcher:
    """A module's watcher: `run(exchange, context)` is awaited after each exchange of the personas it names
    (`personas`), or of every persona when it names none. It runs beside the conversation, never before a
    delivery, and within `timeout_s`; a failure or a timeout is only written in the audit log."""

    name: str
    run: Callable[[Exchange, WatchContext], Awaitable[None]]
    personas: tuple[str, ...] = ()
    timeout_s: float = 120.0

    def __post_init__(self):
        if not NAME.match(str(self.name)):
            raise ValueError(f"A watcher's name is lowercase letters, digits or _, got {self.name!r}")
        if not callable(self.run):
            raise ValueError(f"watcher {self.name!r}: run is an async function of an Exchange and a WatchContext")
        personas = (self.personas,) if isinstance(self.personas, str) else self.personas
        if not isinstance(personas, (tuple, list)) or not all(isinstance(name, str) and name for name in personas):
            raise ValueError(f"watcher {self.name!r}: personas is a list of persona names")
        object.__setattr__(self, "personas", tuple(personas))
        if not isinstance(self.timeout_s, (int, float)) or isinstance(self.timeout_s, bool) or self.timeout_s <= 0:
            raise ValueError(f"watcher {self.name!r}: timeout_s is a positive number of seconds")

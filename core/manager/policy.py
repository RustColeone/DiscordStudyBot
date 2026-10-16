"""Policy data: who may do what, where, and how much. Evaluated by the Manager for every handle call."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any, Mapping

from contracts import Address, Sender


@dataclass(frozen=True)
class Budget:
    model_calls: int = 24
    tool_calls: int = 24
    seconds: float = 900.0

    @classmethod
    def from_config(cls, value: Mapping[str, Any] | None) -> "Budget":
        value = dict(value or {})
        return cls(int(value.get("model_calls", cls.model_calls)), int(value.get("tool_calls", cls.tool_calls)),
                   float(value.get("seconds", cls.seconds)))


@dataclass(frozen=True)
class Assignment:
    """How one conversation (or routine) is handled.

    allow / deny / preapprove are capability-name patterns (`*`, `ext.*`, `ext.action`).
    `trusted` lets routine runs use owner-only capabilities (they have no person sender).
    `lane`: `conversation` (one active run per conversation) or `sender` (one per sender in group
    conversations, so members' requests and answers never mix). `enabled: false` records messages
    without starting runs. `options` are passed to the engine as `session.options`. Requests received
    before `since` (set when the assignment is created or changed at runtime) do not start runs.
    """

    pattern: str
    engine: str
    personas: tuple[str, ...]
    allow: tuple[str, ...] = ("*",)
    deny: tuple[str, ...] = ()
    preapprove: tuple[str, ...] = ()
    budget: Budget = field(default_factory=Budget)
    trusted: bool = False
    lane: str = "conversation"
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)
    since: float = 0.0

    KEYS = frozenset({"engine", "persona", "personas", "allow", "deny", "preapprove", "budget", "trusted",
                      "lane", "enabled", "options", "since", "pattern"})

    @classmethod
    def from_config(cls, pattern: str, value: Mapping[str, Any]) -> "Assignment":
        unknown = set(value) - cls.KEYS
        if unknown:
            raise ValueError(f"Unknown assignment keys {sorted(unknown)}")
        personas = value.get("personas") or ([value["persona"]] if value.get("persona") else [])
        lane = str(value.get("lane", "conversation"))
        if lane not in ("conversation", "sender"):
            raise ValueError("lane must be conversation or sender")
        return cls(pattern=pattern, engine=str(value["engine"]), personas=tuple(personas),
                   allow=tuple(value.get("allow", ("*",))), deny=tuple(value.get("deny", ())),
                   preapprove=tuple(value.get("preapprove", ())), budget=Budget.from_config(value.get("budget")),
                   trusted=bool(value.get("trusted", False)), lane=lane, enabled=bool(value.get("enabled", True)),
                   options=dict(value.get("options") or {}), since=float(value.get("since", 0.0)))

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "engine": self.engine, "personas": list(self.personas),
                "allow": list(self.allow), "deny": list(self.deny), "preapprove": list(self.preapprove),
                "budget": {"model_calls": self.budget.model_calls, "tool_calls": self.budget.tool_calls,
                           "seconds": self.budget.seconds}, "trusted": self.trusted, "lane": self.lane,
                "enabled": self.enabled, "options": dict(self.options), "since": self.since}

    def lane_key(self, request) -> str:
        if self.lane == "sender" and not request.is_direct and request.origin == "person":
            return f"{request.address}#{request.sender.id}"
        return str(request.address)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Assignment":
        return cls.from_config(value["pattern"], value)

    def allows(self, capability: str) -> bool:
        return matches(self.allow, capability) and not matches(self.deny, capability)

    def preapproves(self, capability: str) -> bool:
        return matches(self.preapprove, capability)


def matches(patterns, name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def address_patterns(address: Address) -> list[str]:
    """Most specific first: exact address, then the interface wildcard, then everything."""
    return [str(address), f"{address.interface}:*", "*"]


def most_specific(table: Mapping[str, Any], address: Address):
    for pattern in address_patterns(address):
        if pattern in table:
            return pattern, table[pattern]
    return None, None


class Owners:
    def __init__(self, owners: Mapping[str, Any] | None):
        self._owners = {str(interface): {str(item) for item in (ids or []) if str(item).strip()}
                        for interface, ids in (owners or {}).items()}
        self._owners = {interface: ids for interface, ids in self._owners.items() if ids}
        self._reported: dict[str, set[str]] = {}

    def report(self, interface: str, ids) -> None:
        """An interface's own notion of the owner; configured owners always take precedence."""
        self._reported[interface] = {str(item) for item in ids if str(item).strip()}

    def ids(self, interface: str) -> set[str]:
        return self._owners.get(interface) or self._reported.get(interface, set())

    def is_owner(self, interface: str, sender: Sender) -> bool:
        return sender.kind == "person" and sender.id in self.ids(interface)

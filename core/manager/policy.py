"""Policy data: who may do what, where, and how much. Evaluated by the Manager for every handle call."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
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


LEGACY_KEYS = frozenset({"allow", "deny", "preapprove"})


@dataclass(frozen=True)
class Assignment:
    """How one conversation (or routine) is handled: which engine and personas, with what budget.

    What may be used there and what runs without asking are not part of it: see Permissions.
    `trusted` lets routine runs use owner-only capabilities (they have no person sender).
    `lane`: `conversation` (one active run per conversation) or `sender` (one per sender in group
    conversations, so members' requests and answers never mix). `enabled: false` records messages
    without starting runs. `options` are passed to the engine as `session.options`. Requests received
    before `since` (set when the assignment is created or changed at runtime) do not start runs.
    """

    pattern: str
    engine: str
    personas: tuple[str, ...]
    budget: Budget = field(default_factory=Budget)
    trusted: bool = False
    lane: str = "conversation"
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)
    since: float = 0.0

    KEYS = frozenset({"engine", "persona", "personas", "budget", "trusted", "lane", "enabled", "options", "since",
                      "pattern"})

    @classmethod
    def from_config(cls, pattern: str, value: Mapping[str, Any], *, stored: bool = False) -> "Assignment":
        value = dict(value)
        legacy = LEGACY_KEYS & set(value)
        if legacy and not stored:
            raise ValueError(f"Assignment {pattern!r}: {sorted(legacy)} moved to `permissions:` "
                             f"(allow/deny → use: [\"*\", \"!name\"], preapprove → rules: {{name: auto}}); "
                             "see docs/core/ARCHITECTURE.md, Permissions")
        for key in legacy:
            value.pop(key)  # runs recorded before permissions moved out of assignments
        unknown = set(value) - cls.KEYS
        if unknown:
            raise ValueError(f"Unknown assignment keys {sorted(unknown)}")
        personas = value.get("personas") or ([value["persona"]] if value.get("persona") else [])
        lane = str(value.get("lane", "conversation"))
        if lane not in ("conversation", "sender"):
            raise ValueError("lane must be conversation or sender")
        return cls(pattern=pattern, engine=str(value["engine"]), personas=tuple(personas),
                   budget=Budget.from_config(value.get("budget")), trusted=bool(value.get("trusted", False)),
                   lane=lane, enabled=bool(value.get("enabled", True)), options=dict(value.get("options") or {}),
                   since=float(value.get("since", 0.0)))

    def to_dict(self) -> dict[str, Any]:
        return {"pattern": self.pattern, "engine": self.engine, "personas": list(self.personas),
                "budget": {"model_calls": self.budget.model_calls, "tool_calls": self.budget.tool_calls,
                           "seconds": self.budget.seconds}, "trusted": self.trusted, "lane": self.lane,
                "enabled": self.enabled, "options": dict(self.options), "since": self.since}

    def lane_key(self, request) -> str:
        if self.lane == "sender" and not request.is_direct and request.origin == "person":
            return f"{request.address}#{request.sender.id}"
        return str(request.address)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Assignment":
        return cls.from_config(value["pattern"], value, stored=True)


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

    def interfaces(self) -> list[str]:
        return sorted({*self._owners, *self._reported})

    def is_owner(self, interface: str, sender: Sender) -> bool:
        return sender.kind == "person" and sender.id in self.ids(interface)


# ---- permissions: access, approval ------------------------------------------------------------------
LEVELS = {"member": 0, "admin": 1, "creator": 2}
TIERS = ("read", "prepare", "write", "critical")
TIER_DECISIONS = ("auto", "ask", "always-ask")
RULE_DECISIONS = ("auto", "ask")
BUILTIN_TIERS = {"read": "auto", "prepare": "auto", "write": "ask", "critical": "ask"}
SECTION_KEYS = frozenset({"use", "tiers", "rules"})
SPEC = re.compile(r"^(?P<extension>[a-z][a-z0-9_]*)\.(?P<action>[a-z0-9_*?\[\]]+)(?:\((?P<subject>.+)\))?$")


@dataclass(frozen=True)
class RuleSpec:
    """One capability rule key: `name`, `ext.*` (or another pattern within one extension), or `name(subject)`."""

    text: str
    extension: str
    name: str
    subject: str | None

    @property
    def wildcard(self) -> bool:
        return any(character in self.name for character in "*?[")

    @property
    def rank(self) -> int:
        """Capability specificity: name with subject (3), exact name (2), a pattern (1)."""
        return 1 if self.wildcard else 3 if self.subject is not None else 2

    @classmethod
    def parse(cls, text: str) -> "RuleSpec":
        match = SPEC.match(str(text).strip())
        if match is None:
            raise ValueError(f"Rule {text!r} must name a capability (`ext.action`, `ext.*`, `ext.action(subject)`); "
                             "use tiers for everything at once")
        name = f"{match['extension']}.{match['action']}"
        spec = cls(str(text).strip(), match["extension"], name, match["subject"])
        if spec.wildcard and spec.subject is not None:
            raise ValueError(f"Rule {text!r}: a subject needs an exact capability name")
        return spec


def _path(value: str) -> str:
    path = Path(os.path.expanduser(str(value).strip()))
    if not path.is_absolute():
        path = Path.home() / path
    return os.path.normpath(str(path))


def subject_matches(rule_subject: str, value: str | None, kind: str) -> bool:
    if value is None:
        return False
    if kind == "path":
        return fnmatch.fnmatchcase(_path(value), _path(rule_subject))
    return value == rule_subject


class Permissions:
    """Who may use what (Access) and what runs without asking (Approval), per conversation pattern.

    Built from layers, lowest first (extension code defaults, module config.yml, core.yml); a later layer
    replaces an earlier one setting by setting: a pattern's `use` list, one tier, one rule. Per setting,
    the most specific conversation pattern that sets it wins (exact address, then `interface:*`, then `*`).

    * `use`: capability patterns this conversation may use; `!pattern` excludes. Default: everything
      (the caller's level and the conversation's display still apply).
    * `tiers`: `read`/`prepare`/`write`/`critical` → `auto`, `ask` or `always-ask` (no exceptions: no rule
      or grant applies). `critical` cannot be `auto`.
    * `rules`: capability → `auto` or `ask`, optionally per caller level (`{member: auto}`, `{creator:
      auto}`: that level and above). An explicit rule is final, except that a saved grant may turn its
      `ask` into a run. Patterns (`ext.*`) never make a critical call `auto`; that needs the exact name,
      with the subject when the capability declares one.
    """

    def __init__(self, layers=()):
        self.use: dict[str, tuple[str, ...]] = {}
        self.tiers: dict[str, dict[str, str]] = {}
        self.rules: dict[str, dict[str, tuple[RuleSpec, dict[str, str]]]] = {}
        self.sources: dict[tuple[str, str], str] = {}
        for source, layer in layers:
            self.add(layer, source)

    def add(self, layer: Mapping[str, Any] | None, source: str = "core.yml") -> None:
        for pattern, section in (layer or {}).items():
            parsed = parse_section(str(pattern), section)
            pattern = str(pattern)
            if "use" in parsed:
                self.use[pattern] = parsed["use"]
                self.sources[(pattern, "use")] = source
            for tier, decision in parsed.get("tiers", {}).items():
                self.tiers.setdefault(pattern, {})[tier] = decision
                self.sources[(pattern, "tier:" + tier)] = source
            for key, (spec, levels) in parsed.get("rules", {}).items():
                self.rules.setdefault(pattern, {})[key] = (spec, levels)
                self.sources[(pattern, "rule:" + key)] = source

    # ---- access ---------------------------------------------------------------------------------
    def use_for(self, address: Address) -> tuple[tuple[str, ...], str]:
        for pattern in address_patterns(address):
            if pattern in self.use:
                return self.use[pattern], pattern
        return ("*",), ""

    def allows(self, address: Address, name: str) -> bool:
        patterns, _ = self.use_for(address)
        included = matches([item for item in patterns if not item.startswith("!")], name)
        return included and not matches([item[1:] for item in patterns if item.startswith("!")], name)

    # ---- approval -------------------------------------------------------------------------------
    def tier_setting(self, address: Address, tier: str) -> tuple[str, str]:
        for pattern in address_patterns(address):
            if tier in self.tiers.get(pattern, {}):
                return self.tiers[pattern][tier], pattern
        return BUILTIN_TIERS[tier], ""

    def rule(self, address: Address, name: str, subject: str | None, subject_kind: str, level: str, *,
             critical: bool, has_subject: bool) -> tuple[str, str, str] | None:
        """The rule deciding this call, as (decision, pattern, rule key), or None."""
        for pattern in address_patterns(address):
            candidates = sorted(self.rules.get(pattern, {}).items(),
                                key=lambda item: (-item[1][0].rank, item[1][1].get("member") != "ask"))
            for key, (spec, levels) in candidates:
                if not fnmatch.fnmatchcase(name, spec.name):
                    continue
                if spec.subject is not None and not subject_matches(spec.subject, subject, subject_kind):
                    continue
                if critical and (spec.wildcard or (has_subject and spec.subject is None)):
                    continue
                decision = level_decision(levels, level)
                if decision is not None:
                    return decision, pattern, key
        return None

    def describe(self, address: Address) -> dict[str, Any]:
        """What applies in one conversation, for `$permissions`."""
        use, use_pattern = self.use_for(address)
        tiers = {tier: self.tier_setting(address, tier) for tier in TIERS}
        rules, seen = [], set()
        for pattern in address_patterns(address):
            for key, (spec, levels) in self.rules.get(pattern, {}).items():
                if key in seen:
                    continue
                seen.add(key)
                rules.append({"rule": key, "levels": dict(levels), "pattern": pattern,
                              "source": self.sources.get((pattern, "rule:" + key), "")})
        return {"use": list(use), "use_pattern": use_pattern,
                "tiers": {tier: {"decision": value, "pattern": pattern} for tier, (value, pattern) in tiers.items()},
                "rules": rules}


def level_decision(levels: Mapping[str, str], level: str) -> str | None:
    """The entry for the highest level at or below the caller's."""
    best = None
    for key, decision in levels.items():
        if LEVELS[key] <= LEVELS[level] and (best is None or LEVELS[key] > LEVELS[best[0]]):
            best = (key, decision)
    return best[1] if best else None


def parse_section(pattern: str, section: Any) -> dict[str, Any]:
    """Validate one pattern's permissions; raises ValueError naming what is wrong."""
    if pattern != "*" and ":" not in pattern:
        raise ValueError(f"permissions: pattern {pattern!r} must be '*', 'interface:*' or 'interface:conversation'")
    if not isinstance(section, Mapping):
        raise ValueError(f"permissions {pattern!r}: expected a mapping with use, tiers or rules")
    unknown = set(section) - SECTION_KEYS
    if unknown:
        raise ValueError(f"permissions {pattern!r}: unknown keys {sorted(unknown)} (use, tiers, rules)")
    result: dict[str, Any] = {}
    if "use" in section:
        use = section["use"]
        if isinstance(use, str) or not all(isinstance(item, str) and item.strip() for item in use or ()):
            raise ValueError(f"permissions {pattern!r}: use must be a list of capability patterns")
        result["use"] = tuple(item.strip() for item in use)
    if "tiers" in section:
        tiers = section["tiers"] or {}
        if not isinstance(tiers, Mapping):
            raise ValueError(f"permissions {pattern!r}: tiers must map read/prepare/write/critical to a decision")
        parsed = {}
        for tier, decision in tiers.items():
            if tier not in TIERS:
                raise ValueError(f"permissions {pattern!r}: unknown tier {tier!r}")
            if decision not in TIER_DECISIONS:
                raise ValueError(f"permissions {pattern!r}: tier {tier} must be one of {TIER_DECISIONS}")
            if tier == "critical" and decision == "auto":
                raise ValueError(f"permissions {pattern!r}: critical cannot be auto; name exact rules instead")
            parsed[tier] = decision
        result["tiers"] = parsed
    if "rules" in section:
        rules = section["rules"] or {}
        if not isinstance(rules, Mapping):
            raise ValueError(f"permissions {pattern!r}: rules must map capabilities to auto or ask")
        parsed = {}
        for key, value in rules.items():
            spec = RuleSpec.parse(key)
            levels = {"member": value} if isinstance(value, str) else dict(value or {})
            for level, decision in levels.items():
                if level not in LEVELS:
                    raise ValueError(f"permissions {pattern!r}: rule {key!r}: unknown level {level!r}")
                if decision not in RULE_DECISIONS:
                    raise ValueError(f"permissions {pattern!r}: rule {key!r} must be auto or ask")
            if not levels:
                raise ValueError(f"permissions {pattern!r}: rule {key!r} has no decision")
            parsed[spec.text] = (spec, levels)
        result["rules"] = parsed
    return result


def check_extension_permissions(extension: str, interfaces: set[str], layer: Mapping[str, Any]) -> None:
    """An extension's defaults: `use` and `tiers` only for its own interfaces, rules only for its own
    capabilities (`<extension>.…`, or `<interface>.…` for its interfaces' operations)."""
    own = {extension, *interfaces}
    for pattern, section in (layer or {}).items():
        parsed = parse_section(str(pattern), section)
        if ("use" in parsed or "tiers" in parsed) and str(pattern).split(":", 1)[0] not in interfaces:
            raise ValueError(f"{extension}: permissions may set use and tiers only for its own interfaces, "
                             f"not {pattern!r}")
        for spec, _ in parsed.get("rules", {}).values():
            if spec.extension not in own:
                raise ValueError(f"{extension}: permission rules may cover only its own capabilities, "
                                 f"not {spec.text!r}")

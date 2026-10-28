"""The club: who can take which role in a task, on which models, from which model family.

A seat is one classmate (an agent) with her family and two model lists, `cheap` and `strong`. The seats are the
students (the agents of the students module, as the core describes them: `ExtensionContext.personas`), each on her
own models (her `own` tier: nobody else's as a fallback, so a reviewer's family is never changed silently) and her
strong ones (her `strong` tier). `team:` in the module's config.yml changes a seat key by key, removes one (`false`)
or adds a seat of its own (an agent Chimera defines). Roles are ordered preference lists of seats; a task takes the
first seat that can answer (has a key) and, for a reviewer, comes from a different model family than the drafter.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from contracts import Persona

# The provider a model came from → its family. A reviewer from another family catches different mistakes.
FAMILIES = {"deepseek": "deepseek", "openai": "openai", "gemini": "google", "claude": "anthropic", "grok": "xai"}

# Who may take each role, most preferred first. A drafter is cheap; a judge is a cheap second opinion from another
# family; `strong` takes over a draft that did not pass; a critic is a strong reviewer from another family.
DEFAULT_ROLES = {
    "router": ["gpt", "claude", "gemini", "deepseek", "grok"],
    "drafter": ["deepseek", "gemini", "grok", "gpt", "claude"],
    "judge": ["gemini", "gpt", "deepseek", "claude", "grok"],
    "strong": ["claude", "gpt", "gemini", "deepseek", "grok"],
    "critic": ["claude", "gpt", "gemini", "deepseek", "grok"],
    "tester": ["deepseek", "gemini", "grok", "gpt", "claude"],  # cheapest first: testing costs the most tokens
    "planner": ["claude", "gpt", "gemini", "deepseek", "grok"],  # the plan workflow: strong, so a cheap seat can follow
}
TIERS = {"router": "cheap", "drafter": "cheap", "judge": "cheap", "strong": "strong", "critic": "strong",
         "planner": "strong"}
EFFORTS = ("low", "medium", "high", "max")
# The team picker's formations: its workflow and the roles you seat (the tester seat is a switch on top of any).
FORMATIONS = {"alone": ("single", ("drafter",)), "plan": ("plan", ("planner", "drafter")),
              "cascade": ("cascade", ("drafter", "judge", "strong"))}


@dataclass(frozen=True)
class Seat:
    persona: str
    name: str
    family: str
    voice: str
    cheap: tuple[Mapping[str, Any], ...]
    strong: tuple[Mapping[str, Any], ...]
    avatar: str = ""  # the picture to show (avatars/<avatar>.png); her own name when empty
    also: tuple[str, ...] = ()  # MomoChat personas she stands for in the Classroom (flash for the room's deepseek)
    effort: str = ""  # the most reasoning effort she uses, whatever a call asks (empty: no limit)
    tool_calls: bool = False  # she works through her API's own function calls, not one JSON object per turn

    def profile(self, tier: str) -> str:
        """The model profile Chimera contributes for this seat and tier: only her own models, so a reviewer's
        family is never changed by a silent fallback."""
        return f"chimera_{self.persona}_{tier}"

    def capped(self, effort: str) -> str:
        """The effort a call asks for, or her limit when that is lower (a cut-off answer's retry still asks low)."""
        if self.effort and effort in EFFORTS and EFFORTS.index(self.effort) < EFFORTS.index(effort):
            return self.effort
        return effort

    def models(self, tier: str) -> tuple[Mapping[str, Any], ...]:
        return self.strong if tier == "strong" and self.strong else self.cheap

    def label(self, tier: str) -> str:
        models = self.models(tier)
        return f"{models[0]['provider']}/{models[0]['model']}" if models else ""


@dataclass
class Team:
    seats: dict[str, Seat]
    roles: dict[str, list[str]] = field(default_factory=dict)
    # Seats found unable to answer (no key), with when: later routing names someone who can, for a while.
    unavailable: dict[str, tuple[str, float]] = field(default_factory=dict)
    ready: dict[str, bool] = field(default_factory=dict)  # who has a key, as the latest run could tell (for the page)
    own: set[str] = field(default_factory=set)  # seats `team:` adds, which are not an agent of another module

    MISSING_FOR_S = 600.0

    def mark_unavailable(self, persona: str, reason: str) -> None:
        self.unavailable[persona] = (reason, time.monotonic())

    def known_unavailable(self, persona: str) -> bool:
        entry = self.unavailable.get(persona)
        if entry is None:
            return False
        if time.monotonic() - entry[1] > self.MISSING_FOR_S:
            self.unavailable.pop(persona, None)
            return False
        return True

    def seat(self, persona: str) -> Seat:
        return self.seats[persona]

    def seat_for(self, name: str) -> Seat | None:
        """The seat a classmate's name stands for: her persona or display name, or one a seat is `also` (a MomoChat
        persona she stands for in the Classroom)."""
        key = lambda text: re.sub(r"[^a-z0-9]", "", str(text or "").lower())  # "Flash-chan" is flashchan
        wanted = key(name)
        if not wanted:
            return None
        for seat in self.seats.values():
            if wanted in (key(seat.persona), key(seat.name)):
                return seat
        return next((seat for seat in self.seats.values() if wanted in {key(item) for item in seat.also}), None)

    def candidates(self, role: str, present: set[str] | None = None, prefer: set[str] | frozenset = frozenset()
                   ) -> list[Seat]:
        """Seats that may take `role`, in order, among the personas present in the conversation; those in `prefer`
        (the crew who came along) first, in the same order, whether the role lists them or not."""
        names = list(self.roles.get(role) or self.seats)
        names += [name for name in self.seats if name in prefer and name not in names]
        seats = [self.seats[name] for name in names if name in self.seats and (present is None or name in present)]
        return sorted(seats, key=lambda seat: seat.persona not in prefer) if prefer else seats

    def profiles(self) -> dict[str, list[dict]]:
        return {seat.profile(tier): [dict(item) for item in seat.models(tier)]
                for seat in self.seats.values() for tier in ("cheap", "strong") if seat.models(tier)}

    def personas(self) -> list[Persona]:
        """The agents of the seats `team:` adds (a student's seat is hers: she is the students module's)."""
        return [Persona(seat.persona, seat.name, f"You are {seat.voice}, a member of a school club that builds software "
                        "together. Speak briefly and warmly, in the person's language.", seat.profile("cheap"),
                        description=seat.voice)
                for seat in self.seats.values() if seat.persona in self.own]


def family_of(provider: str, families: Mapping[str, str] | None = None) -> str:
    table = {**FAMILIES, **dict(families or {})}
    return table.get(provider, provider)


def student_seats(agents) -> dict[str, dict]:
    """A seat for each student (the students module's agents, in their order), as `team:` would write it."""
    seats = {}
    for view in agents or ():
        if view.get("module") != "students":
            continue
        models = view.get("models") or {}
        cheap = list(models.get("own") or models.get("default") or ())
        if not cheap:
            continue
        seats[view["name"]] = {"name": view.get("display_name") or view["name"],
                               "voice": view.get("description") or view.get("display_name") or view["name"],
                               "cheap": cheap, "strong": list(models.get("strong") or cheap),
                               "also": [*view.get("aliases", ()), *view.get("formerly", ())]}
    return seats


def build_team(config: Mapping[str, Any], agents=()) -> Team:
    """The team: a seat for each student (`agents`, ExtensionContext.personas), then `team:` (a seat changed key by
    key, `false` removes one, a new name adds a seat of its own; a student is found by her other names too, `luna`
    for GPT), and `roles:` (a role naming a student by another name of hers names her)."""
    seats_config = student_seats(agents)
    own: set[str] = set()

    def named(name: str) -> str:
        wanted = str(name).lower()
        return next((seat for seat, value in seats_config.items()
                     if wanted == seat or wanted in {str(item).lower() for item in value.get("also") or ()}), name)

    for name, value in dict(config.get("team") or {}).items():
        name = named(name)
        if value is False or value is None:
            seats_config.pop(name, None)
            own.discard(name)
        else:
            if name not in seats_config:
                own.add(name)
            seats_config[name] = {**seats_config.get(name, {}), **dict(value)}
    seats = {}
    for name, value in seats_config.items():
        cheap = tuple(_candidates(value.get("cheap") or value.get("models") or ()))
        strong = tuple(_candidates(value.get("strong") or ())) or cheap
        if not cheap:
            raise ValueError(f"Seat {name!r} needs at least one model under cheap")
        effort = str(value.get("effort") or "")
        if effort and effort not in EFFORTS:
            raise ValueError(f"Seat {name!r}: effort is one of {', '.join(EFFORTS)}, got {effort!r}")
        seats[name] = Seat(name, str(value.get("name") or name.capitalize()),
                           str(value.get("family") or family_of(cheap[0]["provider"], config.get("families"))),
                           str(value.get("voice") or value.get("name") or name), cheap, strong,
                           str(value.get("avatar") or name), tuple(str(item).lower() for item in value.get("also") or ()),
                           effort, bool(value.get("tool_calls", False)))
    roles = {key: list(value) for key, value in DEFAULT_ROLES.items()}
    for key, value in dict(config.get("roles") or {}).items():
        roles[key] = [named(str(item)) for item in value]
    return Team(seats, roles, own=own)


def _candidates(items) -> list[dict]:
    """Model candidates as `{provider, model}` mappings or `provider/model` strings."""
    result = []
    for item in items:
        if isinstance(item, str):
            provider, _, model = item.partition("/")
            if not model:
                raise ValueError(f"A model is written provider/model, got {item!r}")
            result.append({"provider": provider, "model": model})
        else:
            result.append(dict(item))
    return result

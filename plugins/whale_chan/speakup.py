"""Speaking up: 鲸酱 sometimes messages first, the way a friend does (after Yuuyuko-uu's dsh-speak-up).

Every `every_min` minutes a local dice roll decides; no model is called unless it hits:

    line   = min(100, base × timing + min(bonus_cap, misses × bonus))
    base   20% in peak hours (weekdays 09–12 and 14–18, when they are busy), 60% otherwise
    timing ×0.15 if she spoke within 20 minutes, ×1.5 after 6 hours of quiet, else ×1
    misses consecutive misses; a hit starts over

She never speaks in quiet hours (1–8), within `cooldown_min` of her own last message, or again before they
answered her last one. A hit becomes a message to her engine marked as her own impulse, with a motive:
something they mentioned coming up (from memory), a feeling she still has (from mood), or a plain one for the
time of day. With `dry_run` (the default) she only writes down what she would have done.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

MARK = "⟨speak-up⟩ "  # starts the text of a speak-up trigger, so history shows it as her impulse

MOTIVES = {
    "plan": "想起{person}说过「{note}」，想问问怎么样了",
    "feeling": "还有点{feeling}，想找{person}说说话",
    "morning": "早上醒来想到{person}，想打个招呼",
    "noon": "中午了，想知道{person}吃饭了没有",
    "evening": "傍晚有点无聊，想{person}了",
    "night": "快到睡觉的时间了，想跟{person}说说话",
    "quiet": "好久没跟{person}说话了，有点想{person}",
}
FEELINGS = {"happy": "开心", "calm": "踏实", "sweet": "甜", "tender": "心疼", "worried": "担心", "sad": "难过",
            "jealous": "吃醋"}


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    dry_run: bool = True
    every_min: float = 30.0
    cooldown_min: float = 20.0
    peak: float = 20.0
    off_peak: float = 60.0
    peak_hours: tuple = ((9, 12), (14, 18))  # weekdays only
    bonus: float = 10.0
    bonus_cap: float = 40.0
    near_min: float = 20.0
    near_factor: float = 0.15
    far_min: float = 360.0
    far_factor: float = 1.5
    quiet: tuple = (1, 8)  # from 01:00 up to 08:00; equal hours turn it off
    max_unanswered: int = 1
    timezone: str = ""
    people: tuple = ()  # Discord user ids; empty: the bot's owners

    @classmethod
    def of(cls, config: Mapping[str, Any] | None) -> "Settings":
        config = dict(config or {})
        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in config.items() if key in known}
        for key in ("quiet", "people"):
            if key in values:
                values[key] = tuple(str(v) if key == "people" else int(v) for v in values[key])
        if "peak_hours" in values:
            values["peak_hours"] = tuple(tuple(int(h) for h in pair) for pair in values["peak_hours"])
        return cls(**values)

    def zone(self):
        return ZoneInfo(self.timezone) if self.timezone else None


@dataclass
class Person:
    her_last: float = 0.0       # her last message to them
    their_last: float = 0.0     # their last message to her
    unanswered: int = 0         # her speak-ups since they last wrote
    misses: int = 0             # dice misses in a row
    log: list = field(default_factory=list)


@dataclass(frozen=True)
class Roll:
    hit: bool
    line: float
    roll: float | None
    reason: str


def local(now: float, settings: Settings) -> datetime:
    zone = settings.zone()
    return datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now).astimezone()


def is_quiet(when: datetime, settings: Settings) -> bool:
    start, end = settings.quiet
    if start == end:
        return False
    hour = when.hour
    return start <= hour < end if start < end else hour >= start or hour < end


def is_peak(when: datetime, settings: Settings) -> bool:
    return when.weekday() < 5 and any(start <= when.hour < end for start, end in settings.peak_hours)


def line(when: datetime, minutes_since_her: float | None, misses: int, settings: Settings) -> tuple[float, str]:
    """The hit line in percent, and how it was reached."""
    base = settings.peak if is_peak(when, settings) else settings.off_peak
    if minutes_since_her is not None and minutes_since_her < settings.near_min:
        timing, why = settings.near_factor, f"she spoke {minutes_since_her:.0f} min ago"
    elif minutes_since_her is None or minutes_since_her > settings.far_min:
        timing, why = settings.far_factor, "quiet for a long time"
    else:
        timing, why = 1.0, f"she spoke {minutes_since_her:.0f} min ago"
    bonus = min(settings.bonus_cap, misses * settings.bonus)
    value = min(100.0, base * timing + bonus)
    return value, (f"{value:.1f} = {'peak' if base == settings.peak else 'off-peak'} {base:g}% × {timing:g} ({why})"
                   + (f" + {bonus:g} for {misses} miss{'es' if misses != 1 else ''}" if bonus else ""))


def decide(person: Person, now: float, settings: Settings, rng: random.Random) -> Roll:
    when = local(now, settings)
    if is_quiet(when, settings):
        return Roll(False, 0.0, None, f"quiet hours ({settings.quiet[0]}–{settings.quiet[1]})")
    if person.unanswered >= settings.max_unanswered:
        return Roll(False, 0.0, None, "waiting for an answer to her last message")
    if person.her_last and now - person.her_last < settings.cooldown_min * 60:
        return Roll(False, 0.0, None, f"cooldown ({settings.cooldown_min:g} min after her own message)")
    since = (now - person.her_last) / 60 if person.her_last else None
    value, how = line(when, since, person.misses, settings)
    roll = rng.uniform(0, 100)
    return Roll(roll < value, value, roll, f"rolled {roll:.1f} against {how}")


def motive(companion, subject: str, name: str, now: float, settings: Settings, silent_h: float = 0.0) -> str:
    """Why she wants to talk, in her own voice: an upcoming plan, a lingering feeling, a long silence, or the
    time of day."""
    if companion is not None:
        try:
            plans = companion.upcoming(subject, now)
            if plans:
                return MOTIVES["plan"].format(person=name, note=plans[0].text[:60])
            feeling = companion.feeling(subject)
            if feeling and feeling[0] in ("worried", "tender", "sad", "sweet"):
                return MOTIVES["feeling"].format(person=name, feeling=FEELINGS[feeling[0]])
        except Exception:
            pass
    if silent_h >= 6:
        return MOTIVES["quiet"].format(person=name)
    hour = local(now, settings).hour
    if hour < 11:
        key = "morning"
    elif hour < 14:
        key = "noon"
    elif hour < 20:
        key = "evening"
    else:
        key = "night"
    return MOTIVES[key].format(person=name)


class Ledger:
    """Who she talks with and when, kept in one JSON file so restarts lose nothing."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.people: dict[str, Person] = {}
        self.overrides: dict[str, Any] = {}
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                self.people = {key: Person(**value) for key, value in data.get("people", {}).items()}
                self.overrides = dict(data.get("overrides") or {})
            except (ValueError, TypeError):
                pass

    def person(self, user_id: str) -> Person:
        return self.people.setdefault(str(user_id), Person())

    def heard(self, user_id: str, at: float) -> None:
        person = self.person(user_id)
        person.their_last, person.unanswered = max(person.their_last, at), 0
        self.save()

    def spoke(self, user_id: str, at: float) -> None:
        person = self.person(user_id)
        person.her_last = max(person.her_last, at)
        self.save()

    def note(self, user_id: str, entry: dict) -> None:
        person = self.person(user_id)
        person.log = (person.log + [entry])[-50:]
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"people": {key: vars(value) for key, value in self.people.items()},
                                         "overrides": self.overrides}, ensure_ascii=False, indent=1))
        temporary.replace(self.path)


class SpeakUp:
    """The dice loop. `submit(user_id, name, motive)` hands a hit to the bot; everything else is local."""

    def __init__(self, settings: Settings, ledger: Ledger, submit, *, owners=lambda: (), names=lambda user: "",
                 companion=None, clock=time.time, rng: random.Random | None = None):
        self.base, self.ledger, self.submit = settings, ledger, submit
        self.owners, self.names, self.companion, self.clock = owners, names, companion, clock
        self.rng = rng or random.Random()

    @property
    def settings(self) -> Settings:
        overrides = {key: value for key, value in self.ledger.overrides.items() if key in ("enabled", "dry_run")}
        return Settings(**{**vars(self.base), **overrides}) if overrides else self.base

    def people(self) -> list[str]:
        return [str(user) for user in (self.settings.people or tuple(self.owners()))]

    async def tick(self) -> list[dict]:
        """One round of dice for everyone; returns what happened (also kept in each person's log)."""
        settings = self.settings
        if not settings.enabled:
            return []
        now, happened = self.clock(), []
        for user in self.people():
            person = self.ledger.person(user)
            result = decide(person, now, settings, self.rng)
            entry = {"at": now, "hit": result.hit, "line": round(result.line, 1), "reason": result.reason}
            if result.roll is not None:
                person.misses = 0 if result.hit else person.misses + 1
            if result.hit:
                entry.update(await self._speak(user, now, dry=settings.dry_run))
            self.ledger.note(user, entry)
            happened.append({"user": user, **entry})
        return happened

    async def now(self, user: str) -> dict:
        """Speak up at once, skipping the dice and the waits (for trying it out)."""
        entry = {"at": self.clock(), "hit": True, "line": 100.0, "reason": "asked to speak up now"}
        entry.update(await self._speak(str(user), entry["at"], dry=False))
        self.ledger.note(str(user), entry)
        return entry

    def rehearse(self, user: str) -> dict:
        """What a roll would do right now, without rolling for real or sending anything."""
        settings, now = self.settings, self.clock()
        person = self.ledger.person(str(user))
        when = local(now, settings)
        since = (now - person.her_last) / 60 if person.her_last else None
        value, how = line(when, since, person.misses, settings)
        blocked = decide(person, now, settings, random.Random(0)) if value else None
        name = self.names(str(user)) or "主人"
        return {"line": round(value, 1), "how": how, "blocked": blocked.reason if blocked and blocked.roll is None
                else "", "motive": motive(self.companion, self._subject(user), name, now, settings,
                                          self._silent_h(person, now)),
                "enabled": settings.enabled, "dry_run": settings.dry_run, "misses": person.misses,
                "unanswered": person.unanswered}

    async def _speak(self, user: str, now: float, dry: bool) -> dict:
        name = self.names(user) or "主人"
        why = motive(self.companion, self._subject(user), name, now, self.settings,
                     self._silent_h(self.ledger.person(user), now))
        if dry:
            return {"dry_run": True, "motive": why}
        try:
            accepted = await self.submit(user, name, why)
        except Exception as error:
            return {"motive": why, "sent": False, "error": f"{type(error).__name__}: {error}"[:200]}
        if accepted:
            person = self.ledger.person(user)
            person.unanswered += 1
        return {"motive": why, "sent": bool(accepted)}

    @staticmethod
    def _silent_h(person: Person, now: float) -> float:
        last = max(person.her_last, person.their_last)
        return (now - last) / 3600 if last else 0.0

    def _subject(self, user: str) -> str:
        return self.companion.subject(user) if self.companion is not None else f"discord:{user}"

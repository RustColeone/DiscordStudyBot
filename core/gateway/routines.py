"""Routines: stored messages the gateway posts on a schedule.

A conversation routine belongs to one conversation and one creator: when due, its text is submitted to
that conversation as if the creator had sent it (`origin="routine"`), so `$status` runs the command and
plain text goes to the conversation's engine. Configured routines (core.yml `routines`) keep their own
pseudo-conversation `routine:<name>`. Times are the host's local time.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from typing import Any, Mapping

DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
UNITS = {"m": 1, "h": 60, "d": 1440}
DURATION = re.compile(r"^(?:(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d))+$", re.I)
PART = re.compile(r"(\d+(?:\.\d+)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)", re.I)
CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
SCHEDULE_KEYS = ("every_minutes", "daily_at", "days", "once_at")
MAX_TEXT = 2000


def minutes(text: str) -> float | None:
    compact = text.replace(" ", "")
    if not DURATION.match(compact):
        return None
    return sum(float(value) * UNITS[unit[0].lower()] for value, unit in PART.findall(compact))


def parse_days(text: str) -> list[int] | None:
    text = text.lower()
    if text in ("daily", "everyday", "every-day"):
        return []
    if text == "weekdays":
        return [0, 1, 2, 3, 4]
    if text == "weekends":
        return [5, 6]
    days = []
    for part in text.split(","):
        key = part.strip()[:3]
        if key not in DAYS:
            return None
        days.append(DAYS[key])
    return sorted(set(days))


def parse(text: str, now: float) -> tuple[dict, str]:
    """`every 1h <text>`, `daily 08:00 <text>`, `weekdays 09:00 <text>`, `mon,wed 18:30 <text>`,
    `at 18:30 <text>` or `at 2026-10-17 08:00 <text>`. Returns (schedule, text)."""
    tokens = text.strip().split()
    if len(tokens) < 2:
        raise ValueError("Give a schedule and what to send, e.g. `every 1h $status` or `daily 08:00 good morning`")
    head = tokens[0].lower()
    if head == "every":
        # `every 2h30m …`, `every 2 h …`, `every 90 minutes …`
        for take in (1, 2, 3):
            amount = minutes("".join(tokens[1:1 + take]))
            if amount is not None and len(tokens) > 1 + take:
                return {"every_minutes": amount}, " ".join(tokens[1 + take:])
        raise ValueError("Write the interval like `every 30m`, `every 2h` or `every 1d`")
    if head == "at":
        if len(tokens) >= 4 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", tokens[1]) and CLOCK.match(tokens[2]):
            moment = dt.datetime.fromisoformat(f"{tokens[1]}T{tokens[2]}").astimezone()
            return {"once_at": moment.timestamp()}, " ".join(tokens[3:])
        if CLOCK.match(tokens[1]) and len(tokens) >= 3:
            return {"once_at": next_clock(tokens[1], [], now)}, " ".join(tokens[2:])
        raise ValueError("Write a one-off time like `at 18:30` or `at 2026-10-17 08:00`")
    days = parse_days(head)
    if days is not None and len(tokens) >= 3 and CLOCK.match(tokens[1]):
        schedule = {"daily_at": tokens[1]}
        if days:
            schedule["days"] = days
        return schedule, " ".join(tokens[2:])
    raise ValueError("Start with `every 1h`, `daily 08:00`, `weekdays 09:00`, `mon,wed 18:30` or `at 18:30`")


def next_clock(clock: str, days: list[int], now: float) -> float:
    hour, minute = (int(part) for part in clock.split(":"))
    current = dt.datetime.fromtimestamp(now).astimezone()
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(8):
        if candidate.timestamp() > now and (not days or candidate.weekday() in days):
            return candidate.timestamp()
        candidate += dt.timedelta(days=1)
    raise ValueError("No matching day for this schedule")


def next_fire(routine: Mapping[str, Any], now: float) -> float | None:
    """The next time after `now`, or None when a one-off is done."""
    if routine.get("once_at") is not None:
        return float(routine["once_at"]) if float(routine["once_at"]) > now else None
    if routine.get("every_minutes"):
        return now + float(routine["every_minutes"]) * 60
    return next_clock(str(routine["daily_at"]), list(routine.get("days") or []), now)


def describe(routine: Mapping[str, Any]) -> str:
    if routine.get("once_at") is not None:
        return "once at " + dt.datetime.fromtimestamp(float(routine["once_at"])).astimezone().strftime("%Y-%m-%d %H:%M")
    if routine.get("every_minutes"):
        value = float(routine["every_minutes"])
        if value % 1440 == 0:
            return f"every {int(value // 1440)}d"
        if value % 60 == 0:
            return f"every {int(value // 60)}h"
        return f"every {value:g}m"
    days = list(routine.get("days") or [])
    when = ("daily" if not days else "weekdays" if days == [0, 1, 2, 3, 4] else "weekends" if days == [5, 6]
            else ",".join(DAY_NAMES[day] for day in days))
    return f"{when} {routine['daily_at']}"


def validate(schedule: Mapping[str, Any]) -> dict:
    schedule = {key: value for key, value in dict(schedule).items() if key in SCHEDULE_KEYS and value is not None}
    kinds = [key for key in ("every_minutes", "daily_at", "once_at") if key in schedule]
    if len(kinds) != 1:
        raise ValueError("Give exactly one of every_minutes, daily_at or once_at")
    if "every_minutes" in schedule and not float(schedule["every_minutes"]) >= 1:
        raise ValueError("The interval must be at least one minute")
    if "daily_at" in schedule and not CLOCK.match(str(schedule["daily_at"])):
        raise ValueError("daily_at must be HH:MM")
    if "days" in schedule:
        if "daily_at" not in schedule or not all(isinstance(day, int) and 0 <= day <= 6 for day in schedule["days"]):
            raise ValueError("days (0 = Monday … 6 = Sunday) go with daily_at")
    return schedule


def new_id() -> str:
    return "r" + secrets.token_hex(3)

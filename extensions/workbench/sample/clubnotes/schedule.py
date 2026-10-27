"""Reading a weekly schedule and finding the next meeting."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class Meeting:
    day: str
    time: str  # HH:MM
    name: str

    def key(self) -> tuple[int, str]:
        # Python's isoweekday() counts Monday as 1 and Sunday as 7; this uses % 7, so Sunday sorts as day 0.
        return ((DAYS.index(self.day) + 1) % 7, self.time)

    def __str__(self) -> str:
        return f"{self.day} {self.time} {self.name}"


def parse(text: str) -> list[Meeting]:
    """One meeting per line: `Mon 15:30 Chess`. Blank lines and lines starting with # are skipped."""
    meetings = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 3 or parts[0] not in DAYS:
            raise ValueError(f"line {number}: expected 'Day HH:MM Name', got {line!r}")
        meetings.append(Meeting(parts[0], parts[1], parts[2]))
    return meetings


def sort_week(meetings: list[Meeting]) -> list[Meeting]:
    """Meetings in week order, Monday first."""
    return sorted(meetings, key=Meeting.key)


def next_meeting(meetings: list[Meeting], now: dt.datetime) -> Meeting:
    """The first meeting at or after `now`, wrapping around to next week."""
    today = now.weekday()
    current = now.strftime("%H:%M")
    def wait(meeting: Meeting) -> tuple[int, str]:
        days = (DAYS.index(meeting.day) - today) % 7
        if days == 0 and meeting.time < current:
            days = 7
        return (days, meeting.time)
    return min(meetings, key=wait)

"""The lab as a place, in the shape every interface understands (contracts.PLACE_STATUS): who is here, who came from
elsewhere, and which invitations wait. The lab's interface publishes it, so the classroom (or any other interface)
can draw the lab on its map without knowing anything about the lab.

A visit lasts from the start of a task until it is reported back or closed (or `visit_minutes` after it last moved,
once nothing runs): the classmate who brought you, her crew and whoever worked on it are here meanwhile. Someone
holding an invitation you have not followed yet stays where she is, with a ticket.
"""

from __future__ import annotations

import re
import time
from typing import Callable

DOING = {"review": "waiting for your check", "approved": "waiting for the final approval", "landed": "just landed it",
         "answered": "answered", "failed": "waiting for you"}


def key(name) -> str:
    """A name as places compare them (contracts.PLACE_STATUS): lowercased, its letters and digits in any script
    ("Flash-chan" → "flashchan", "露娜" → "露娜")."""
    return re.sub(r"[\W_]", "", str(name or "").lower())[:40]


def lab_place(board, team, link: Callable[[str, str], str], visit_minutes: float = 30) -> dict:
    now, stay = time.time(), float(visit_minutes) * 60
    inside: dict[str, dict] = {}
    guests: set[str] = set()   # names (as keys) from elsewhere who are here now
    tickets, ready, busy = [], 0, 0
    for task in board.tasks(200):
        data, status = task["data"], task["status"]
        invited = data.get("invited") or {}
        if status == "invited":
            seat = team.seat_for(invited.get("lead") or invited.get("by") or "")
            by = str(invited.get("by") or "")
            # her own picture first (the classroom's DeepSeek), else the seat standing in for her
            tickets.append({"key": key(by) or (seat.persona if seat else ""), "by": by, "title": task["title"],
                            "task": task["id"],
                            "avatar": key(by) or (seat.avatar if seat else ""), "link": link(task["id"], "")})
            continue
        ready += status in ("review", "approved")
        live = status in ("queued", "working") and now - task["updated"] < max(stay, 7200)  # (not a stuck one)
        if not live and (status not in DOING or data.get("reported") or now - task["updated"] > stay):
            continue  # this visit is over
        steps = board.steps(task["id"])
        running = {step["seat"] for step in steps if step["status"] == "running" and step["seat"]}
        people = [invited.get("lead") or "", *(data.get("crew", invited.get("crew")) or ()),
                  *(step["seat"] for step in steps)]
        for persona in dict.fromkeys(people):
            if persona not in team.seats or persona in inside or team.ready.get(persona) is False:
                continue  # (one without a key could not come)
            busy += persona in running
            inside[persona] = {"doing": (task["phase"] or "working") if persona in running
                               else DOING.get(status, "with the team"), "title": task["title"], "task": task["id"]}
        if invited.get("by"):
            guests.add(key(invited["by"]))
    people = []
    for persona, visit in inside.items():
        seat = team.seats[persona]
        guests |= {persona, key(seat.name), *(key(name) for name in seat.also)}
        people.append({"key": persona, "name": seat.name, "avatar": seat.avatar or persona, **visit})
    note = " · ".join(part for part in (f"{busy} at work" if busy else "", f"{ready} for you to check" if ready else "")
                      if part) or ("on a visit" if people else "quiet")
    return {"place": {"name": "Lab", "icon": "🧪", "note": note}, "people": people, "away": sorted(guests - {""}),
            "tickets": tickets[:6], "working": busy, "ready": ready}

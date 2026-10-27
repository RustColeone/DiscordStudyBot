"""Built-in capabilities: current time and simple notes, kept per conversation (exercises storage and
approvals). People save notes (`$remember`); models may read them (`basic.notes`), not write them."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contracts import Capability, Contributions, Extension, Risk, ToolResult


async def now(arguments, context) -> ToolResult:
    name = arguments.get("timezone")
    try:
        zone = ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):  # ValueError: not even shaped like a name ("../x")
        return ToolResult.fail(f"Unknown IANA timezone {name!r}", "invalid_timezone")
    current = dt.datetime.now(zone).astimezone(zone) if zone else dt.datetime.now().astimezone()
    return ToolResult.ok(current.strftime("%Y-%m-%d %H:%M:%S %A %Z (UTC%z)"),
                         data={"iso": current.isoformat(), "timestamp": time.time()})


KEEP = 200  # notes kept per conversation, newest last
SHARED = "notes"  # notes saved before they were kept per conversation: shown to the creator only


def key(context) -> str:
    return f"notes:{context.address}"  # each conversation keeps its own notes


async def remember(arguments, context) -> ToolResult:
    text = arguments["text"].strip()
    if not text:
        return ToolResult.fail("Nothing to remember", "empty")
    notes = context.storage.get(key(context), [])
    notes.append({"text": text[:2000], "at": time.time(), "by": context.sender.name or context.sender.id})
    notes = notes[-KEEP:]
    context.storage.set(key(context), notes)
    return ToolResult.ok(f"Saved note #{len(notes)}.")


def listing(stored: list) -> list[str]:
    return [f"{index}. {item['text']}" for index, item in enumerate(stored[-20:], start=max(1, len(stored) - 19))]


async def notes(arguments, context) -> ToolResult:
    stored = context.storage.get(key(context), [])
    shared = context.storage.get(SHARED, []) if context.access == "creator" else []
    if not stored and not shared:
        return ToolResult.ok("No notes here yet.", data={"notes": []})
    lines = listing(stored) or ["No notes here yet."]
    if shared:
        lines += ["", "Earlier notes (from before notes were kept per conversation):", *listing(shared)]
    return ToolResult.ok("\n".join(lines), data={"notes": stored[-20:], "earlier": shared[-20:]})


def setup(context) -> Contributions:
    return Contributions(capabilities=[
        Capability("basic.now", "Current local date and time, optionally in an IANA timezone.", now,
                   {"timezone": {"type": "string", "description": "IANA name such as Asia/Shanghai"}},
                   risk=Risk.READ, command="now", command_argument="timezone"),
        # Typed by people only (`$remember`): offered to models, it was taken for a memory of their own, a write
        # that waits for the owner each time, while the companion module keeps what a student remembers.
        Capability("basic.remember", "Save a short note for later, in this conversation.", remember,
                   {"text": {"type": "string"}}, ("text",), Risk.WRITE, command="remember",
                   command_argument="text", internal=True),
        Capability("basic.notes", "List the notes saved in this conversation.", notes, risk=Risk.READ,
                   command="notes"),
    ])


EXTENSION = Extension("basic", setup, contracts="1", version="1", description="Time and notes")

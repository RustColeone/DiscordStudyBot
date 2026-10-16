"""Built-in capabilities: current time and simple owner notes (exercises storage and approvals)."""

from __future__ import annotations

import datetime as dt
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contracts import Capability, Contributions, Extension, Risk, ToolResult


async def now(arguments, context) -> ToolResult:
    name = arguments.get("timezone")
    try:
        zone = ZoneInfo(name) if name else None
    except ZoneInfoNotFoundError:
        return ToolResult.fail(f"Unknown IANA timezone {name!r}", "invalid_timezone")
    current = dt.datetime.now(zone).astimezone(zone) if zone else dt.datetime.now().astimezone()
    return ToolResult.ok(current.strftime("%Y-%m-%d %H:%M:%S %A %Z (UTC%z)"),
                         data={"iso": current.isoformat(), "timestamp": time.time()})


async def remember(arguments, context) -> ToolResult:
    text = arguments["text"].strip()
    if not text:
        return ToolResult.fail("Nothing to remember", "empty")
    notes = context.storage.get("notes", [])
    notes.append({"text": text[:2000], "at": time.time(), "by": context.sender.name or context.sender.id})
    context.storage.set("notes", notes[-200:])
    return ToolResult.ok(f"Saved note #{len(notes)}.")


async def notes(arguments, context) -> ToolResult:
    stored = context.storage.get("notes", [])
    if not stored:
        return ToolResult.ok("No notes yet.", data={"notes": []})
    lines = [f"{index}. {item['text']}" for index, item in enumerate(stored[-20:], start=max(1, len(stored) - 19))]
    return ToolResult.ok("\n".join(lines), data={"notes": stored[-20:]})


def setup(context) -> Contributions:
    return Contributions(capabilities=[
        Capability("basic.now", "Current local date and time, optionally in an IANA timezone.", now,
                   {"timezone": {"type": "string", "description": "IANA name such as Asia/Shanghai"}},
                   risk=Risk.READ, command="now", command_argument="timezone"),
        Capability("basic.remember", "Save a short note for later.", remember,
                   {"text": {"type": "string"}}, ("text",), Risk.WRITE, command="remember",
                   command_argument="text"),
        Capability("basic.notes", "List saved notes.", notes, risk=Risk.READ, command="notes"),
    ])


EXTENSION = Extension("basic", setup, contracts="1", version="1", description="Time and notes")

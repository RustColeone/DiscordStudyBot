"""Reminders: one-time messages delivered later to the conversation that asked for them."""

from __future__ import annotations

import datetime as dt
import re
import time

from contracts import Capability, Command, Contributions, Extension, Option, Risk, ToolResult

UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)(?=\d|\s|$)",
                      re.I)


def local_text(at: float) -> str:
    return dt.datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def parse_spec(spec: str, now: float) -> tuple[float, str]:
    """`10m drink water`, `2h30m stretch`, `18:30 call mom` or an ISO time followed by the text."""
    spec = spec.strip()
    total, rest = 0.0, spec
    while (match := DURATION.match(rest)):
        total += float(match.group(1)) * UNITS[match.group(2)[0].lower()]
        rest = rest[match.end():].strip()
    if total:
        return now + total, rest
    first, _, rest = spec.partition(" ")
    if re.fullmatch(r"\d{1,2}:\d{2}", first):
        hour, minute = map(int, first.split(":"))
        current = dt.datetime.fromtimestamp(now).astimezone()
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += dt.timedelta(days=1)
        return target.timestamp(), rest.strip()
    return parse_iso(first), rest.strip()


def parse_iso(text: str) -> float:
    try:
        moment = dt.datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"Not a time: {text!r}. Use e.g. 10m, 2h, 18:30 or 2026-10-16T08:00+08:00") from None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.timestamp()


async def create(arguments, context) -> ToolResult:
    now = time.time()
    try:
        if arguments.get("spec"):
            at, text = parse_spec(arguments["spec"], now)
            text = text or arguments.get("text", "")
        else:
            text = arguments.get("text", "")
            if arguments.get("at"):
                at = parse_iso(arguments["at"])
            elif arguments.get("in_minutes") is not None:
                at = now + float(arguments["in_minutes"]) * 60
            else:
                return ToolResult.fail("Give `at` (ISO 8601 with offset) or `in_minutes`.", "invalid_arguments")
        if not text.strip():
            return ToolResult.fail("What should the reminder say?", "invalid_arguments")
        scheduled_id = await context.schedule(at, text.strip())
    except ValueError as error:
        return ToolResult.fail(str(error), "invalid_arguments")
    return ToolResult.ok(f"Reminder {scheduled_id} set for {local_text(at)}: {text.strip()}",
                         data={"id": scheduled_id, "at": at})


async def list_reminders(arguments, context) -> ToolResult:
    items = context.scheduled()
    if not items:
        return ToolResult.ok("No pending reminders here.", data={"reminders": []})
    lines = [f"{item['id']} · {local_text(item['at'])} · {item['text']} ({item['sender_name'] or item['sender_id']})"
             for item in items]
    return ToolResult.ok("\n".join(lines), data={"reminders": items})


async def cancel(arguments, context) -> ToolResult:
    item = next((item for item in context.scheduled() if item["id"] == arguments["id"]), None)
    if item is None:
        return ToolResult.fail(f"No pending reminder {arguments['id']} here.", "not_found")
    # The setter, or an admin. A routine acts as the person who set it up (and an owner's trusted one as the
    # creator), so it gets no more than they would.
    if item["sender_id"] != context.sender.id and context.access not in ("admin", "creator"):
        return ToolResult.fail("Only the person who set a reminder (or an admin) can cancel it.", "not_permitted")
    await context.unschedule(item["id"])
    return ToolResult.ok(f"Reminder {item['id']} cancelled.")


def setup(context) -> Contributions:
    return Contributions(capabilities=[
        Capability("reminders.create",
                   "Set a one-time reminder in this conversation. Give text and either `at` (ISO 8601 with UTC offset, "
                   "computed from the current time) or `in_minutes`. `spec` is the $remind form, e.g. '10m drink water'.",
                   create, {"text": {"type": "string"}, "at": {"type": "string"},
                            "in_minutes": {"type": "number", "exclusiveMinimum": 0}, "spec": {"type": "string"}},
                   risk=Risk.WRITE, command=Command(
                       "remind", "Set a one-time reminder here",
                       (Option("when", "In how long or when: 10m, 2h30m, 18:30, or a date and time", required=True),
                        Option("text", "What to remind you of", required=True)),
                       template="{when} {text}", examples=("10m drink water", "18:30 call mom")),
                   command_argument="spec"),
        Capability("reminders.list", "List pending reminders in this conversation.", list_reminders,
                   risk=Risk.READ, command="reminders"),
        Capability("reminders.cancel", "Cancel a pending reminder by its ID.", cancel, {"id": {"type": "string"}},
                   ("id",), Risk.WRITE, command=Command("unremind", "Cancel one of your reminders",
                                                        (Option("id", "Reminder ID", required=True),)),
                   command_argument="id"),
    ])


EXTENSION = Extension("reminders", setup, contracts="1", version="1", description="One-time reminders")

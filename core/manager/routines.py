"""Who may create, see and change conversation routines; shared by `$routine` and the core.* routine
capabilities. Members manage their own routines in the conversation they are in, within limits; admins
and the creator see and manage every conversation's routines and have no limits."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from contracts import Address, Sender
from core.gateway import routines as schedules

if TYPE_CHECKING:
    from core.manager.manager import Manager

MEMBER_MAX_PER_CONVERSATION = 5
MEMBER_MIN_MINUTES = 30
LEVELS = {"member": 0, "admin": 1, "creator": 2}


@dataclass(frozen=True)
class Caller:
    address: Address
    sender: Sender
    is_direct: bool
    access: str
    markdown: bool = True

    @property
    def admin(self) -> bool:
        return LEVELS[self.access] >= LEVELS["admin"]


class RoutineDesk:
    def __init__(self, manager: "Manager"):
        self._manager = manager

    @property
    def _gateway(self):
        return self._manager.gateway

    def add(self, caller: Caller, schedule_text: str, text: str, chat: str | None = None,
            via: str = "command") -> dict:
        """`schedule_text` is `every 1h`, `daily 08:00`, … ; returns the stored routine. `via` is `command`
        when a person typed it, `tool` when a model set it up (its firings then carry no approval)."""
        address, is_direct = caller.address, caller.is_direct
        if chat:
            if not caller.admin:
                raise PermissionError("Only admins can add routines to another conversation")
            address, is_direct = Address.parse(chat), chat.split(":", 1)[-1].startswith("dm:")
        schedule, rest = schedules.parse(f"{schedule_text} {text}".strip(), self._gateway.now())
        if not caller.admin:
            if schedule.get("every_minutes") and float(schedule["every_minutes"]) < MEMBER_MIN_MINUTES:
                raise PermissionError(f"Routines repeat at most every {MEMBER_MIN_MINUTES} minutes")
            mine = [item for item in self._gateway.conversation_routines(address)
                    if item["sender"]["id"] == caller.sender.id]
            if len(mine) >= MEMBER_MAX_PER_CONVERSATION:
                raise PermissionError(f"You already have {MEMBER_MAX_PER_CONVERSATION} routines here; "
                                      "delete one first")
        return self._gateway.add_routine(address, caller.sender, is_direct, schedule, rest, via=via)

    def visible(self, caller: Caller) -> list[dict]:
        if caller.admin:
            items = self._gateway.conversation_routines()
            if caller.access == "creator":
                items += self._gateway.configured_routines()
            return items
        return self._gateway.conversation_routines(caller.address)

    def change(self, caller: Caller, routine_id: str, action: str) -> dict:
        routine = next((item for item in [*self._gateway.conversation_routines(),
                                          *self._gateway.configured_routines()]
                        if item["id"] == routine_id.strip().lstrip("#")), None)
        if routine is None:
            raise LookupError(f"No routine {routine_id!r}")
        if not routine.get("address"):
            allowed = caller.access == "creator"
        elif caller.admin:
            allowed = True
        else:
            allowed = (routine["address"] == str(caller.address)
                       and routine["sender"]["id"] == caller.sender.id)
        if not allowed:
            raise PermissionError("You can only change routines you created in this conversation")
        return self._gateway.update_routine(routine["id"], enabled=action == "resume", delete=action == "delete")

    def line(self, caller: Caller, routine: dict) -> str:
        code = (lambda value: f"`{value}`") if caller.markdown else str
        upcoming = routine.get("next_at")
        when = dt.datetime.fromtimestamp(upcoming).astimezone().strftime("%m-%d %H:%M") if upcoming else "—"
        state = "" if routine.get("enabled", True) else " · ⏸ paused"
        text = routine["text"] if len(routine["text"]) <= 80 else routine["text"][:79] + "…"
        parts = [code(routine["id"]), schedules.describe(routine), f"next {when}", code(text)]
        if caller.admin:
            where = routine.get("address") or f"routine:{routine['id']}"
            owner = routine.get("sender", {}).get("name") or routine.get("sender", {}).get("id") or "config"
            parts.append(f"{where} · {owner}")
        return " · ".join(parts) + state

    def listing(self, caller: Caller) -> str:
        items = self.visible(caller)
        if not items:
            return ("No routines anywhere." if caller.admin else
                    "No routines in this conversation. Add one with `$routine every 1h <message or $command>`.")
        title = "All routines" if caller.admin else "Routines here"
        return "\n".join([f"{title}:", *(self.line(caller, item) for item in items)])

    def command(self, caller: Caller, text: str, via: str = "command") -> str:
        """`$routine [list] | [add] [--chat <address>] <schedule> <text> | delete|pause|resume <id>`."""
        tokens = text.split()
        chat = None
        if "--chat" in tokens:
            index = tokens.index("--chat")
            if index + 1 >= len(tokens):
                return "Usage: `--chat <interface:conversation>`."
            chat = tokens[index + 1]
            tokens = tokens[:index] + tokens[index + 2:]
        action = tokens[0].lower() if tokens else "list"
        if action == "list":
            return self.listing(caller)
        try:
            if action in ("delete", "pause", "resume"):
                if len(tokens) != 2:
                    return f"Usage: `$routine {action} <id>` (see `$routine`)."
                routine = self.change(caller, tokens[1], action)
                done = {"delete": "deleted", "pause": "paused", "resume": "resumed"}[action]
                return f"Routine {routine['id']} {done}."
            if action == "add":
                tokens = tokens[1:]
            routine = self.add(caller, "", " ".join(tokens), chat, via=via)
        except (ValueError, LookupError, PermissionError) as error:
            return str(error)
        when = dt.datetime.fromtimestamp(routine["next_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
        return (f"Routine {routine['id']} added: {schedules.describe(routine)}, first at {when}"
                + (f" in {routine['address']}" if chat else "") + f". It sends: {routine['text']}")

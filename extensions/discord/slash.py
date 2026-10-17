"""Translation between Discord's native commands and the core's `$` commands.

The core only knows `$name <text>`. This module turns the core's command catalog into Discord
application commands (slash commands, and message actions under Apps), turns an invocation or a
button press back into that text, and keeps the interaction so replies can answer it: the first
reply replaces the "thinking…" placeholder, later ones follow up, private replies are ephemeral.
Interactions can be answered for 15 minutes; after that replies go to the channel as usual.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from contracts import Command

OPTION_TYPES = {"string": 3, "integer": 4, "boolean": 5, "number": 10, "attachment": 11}
CHAT_INPUT, MESSAGE = 1, 3
BUTTON_PREFIX = "cmd:"
INTERACTION_LIFETIME_S = 14 * 60
URL = re.compile(r"https?://\S+")


def payload(commands: list[Command]) -> list[dict]:
    """Application-command definitions for a bulk overwrite (chat commands, then message actions)."""
    result = []
    for command in commands:
        options = sorted(command.options, key=lambda option: not option.required)
        definition = {
            "type": CHAT_INPUT, "name": command.name,
            "description": (command.description or command.name)[:100],
            "options": [{"type": OPTION_TYPES[option.type], "name": option.name,
                         "description": (option.description or option.name)[:100], "required": option.required,
                         **({"choices": [{"name": choice[:100], "value": choice} for choice in option.choices[:25]]}
                            if option.choices and option.type == "string" else {})}
                        for option in options][:25],
            "contexts": [0, 1, 2], "integration_types": [0],
        }
        if command.access == "creator":
            # Hidden from members in servers (server admins still see it); the core enforces access anyway.
            definition["default_member_permissions"] = "0"
        result.append(definition)
    actions = [command for command in commands if command.message_action][:5]
    result += [{"type": MESSAGE, "name": command.message_action[:32], "contexts": [0, 1, 2],
                "integration_types": [0]} for command in actions]
    return result


def fingerprint(definitions: list[dict]) -> str:
    return hashlib.sha256(json.dumps(definitions, sort_keys=True).encode()).hexdigest()


def find(commands: list[Command], data: dict) -> Command | None:
    if data.get("type", CHAT_INPUT) == MESSAGE:
        return next((command for command in commands if command.message_action[:32] == data.get("name")), None)
    return next((command for command in commands if command.name == data.get("name")), None)


def values(command: Command, data: dict) -> tuple[dict, list[dict]]:
    """Option values by name, and the raw attachment objects chosen for attachment options."""
    if data.get("type", CHAT_INPUT) == MESSAGE:
        message = (data.get("resolved", {}).get("messages", {}) or {}).get(str(data.get("target_id")), {})
        content = str(message.get("content") or "")
        first = next((option for option in command.options if option.type == "string"), None)
        if first is None:
            return {}, []
        link = URL.search(content)
        value = link.group(0) if link and first.name in ("url", "link") else content[:1000]
        return {first.name: value}, list(message.get("attachments") or [])
    found, files = {}, []
    resolved = (data.get("resolved") or {}).get("attachments") or {}
    for option in data.get("options") or []:
        if option.get("type") == OPTION_TYPES["attachment"]:
            attachment = resolved.get(str(option.get("value")))
            if attachment:
                files.append(attachment)
            continue
        found[option["name"]] = option.get("value")
    return found, files


@dataclass
class Pending:
    interaction: Any
    conversation: str
    at: float = field(default_factory=time.monotonic)
    placeholder: bool = True  # the deferred "thinking…" response has not been used for an answer yet

    @property
    def fresh(self) -> bool:
        return time.monotonic() - self.at < INTERACTION_LIFETIME_S


class Interactions:
    """Interactions waiting for replies, by the request ID they became."""

    def __init__(self, limit: int = 500):
        self._items: OrderedDict[str, Pending] = OrderedDict()
        self._limit = limit

    def add(self, request_id: str, interaction: Any, conversation: str, placeholder: bool = True) -> Pending:
        pending = self._items[request_id] = Pending(interaction, conversation, placeholder=placeholder)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)
        return pending

    def get(self, request_id: str | None) -> Pending | None:
        pending = self._items.get(request_id) if request_id else None
        if pending is not None and not pending.fresh:
            self._items.pop(request_id, None)
            return None
        return pending

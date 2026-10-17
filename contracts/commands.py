"""Commands: `$name <text>` requests, declared so every interface can offer them in its own way.

The core only ever sees the text form (`$clip -u https://… -s 1:05`). A declaration adds what an
interface with a native command UI needs (Discord slash commands): a description, typed options, and a
`template` that turns option values back into the text. Text-only interfaces ignore it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

Access = Literal["member", "admin", "creator"]
OptionType = Literal["string", "integer", "number", "boolean", "attachment"]
NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
PLACEHOLDER = re.compile(r"\{([a-z0-9_-]+)\}")
OPTIONAL = re.compile(r"\[([^\[\]]*)\]")


@dataclass(frozen=True)
class Option:
    """One argument. `attachment` options are delivered as the request's files, not in the text. A
    boolean option with a `flag` renders as that flag when true (`--fit`) and as nothing when false."""

    name: str
    description: str = ""
    type: OptionType = "string"
    required: bool = False
    choices: tuple[str, ...] = ()
    flag: str = ""

    def __post_init__(self):
        if not NAME.match(self.name):
            raise ValueError(f"Option names are 1-32 lowercase letters, digits, _ or -: {self.name!r}")
        object.__setattr__(self, "choices", tuple(str(choice) for choice in self.choices))


@dataclass(frozen=True)
class Command:
    """A command declaration.

    `template` renders option values into the text after `$name`: `{option}` is replaced by its value
    and `[...]` sections are dropped when an option inside them has no value, e.g.
    `-u {url}[ -s {start}][ -e {end}]`. Without a template, values are joined with spaces in option
    order. `examples` are argument texts shown in help (`10m drink water`). `access` is the lowest level
    that may run it; `admin_help` is extra help shown to admins and the creator only. `message_action`,
    when set, is a label for acting on an existing message (its text fills the first option).
    """

    name: str
    description: str = ""
    options: tuple[Option, ...] = ()
    template: str = ""
    examples: tuple[str, ...] = ()
    access: Access = "member"
    admin_help: str = ""
    message_action: str = ""

    def __post_init__(self):
        if not NAME.match(self.name):
            raise ValueError(f"Command names are 1-32 lowercase letters, digits, _ or -: {self.name!r}")
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "examples", tuple(self.examples))
        known = {option.name for option in self.options}
        unknown = set(PLACEHOLDER.findall(self.template)) - known
        if unknown:
            raise ValueError(f"${self.name}: template uses undeclared options {sorted(unknown)}")
        if self.access not in ("member", "admin", "creator"):
            raise ValueError(f"${self.name}: access must be member, admin or creator")

    def render(self, values: Mapping[str, Any]) -> str:
        """The `$name …` text for these option values (attachments are not part of the text)."""
        flags = {option.name: option.flag for option in self.options if option.flag}
        present = {}
        for key, value in values.items():
            if key in flags:
                if value is True or _text(value) == "true":
                    present[key] = flags[key]
            elif value is not None and _text(value) != "":
                present[key] = _text(value)
        if self.template:
            def section(match: re.Match) -> str:
                inner = match.group(1)
                return inner if all(name in present for name in PLACEHOLDER.findall(inner)) else ""
            body = OPTIONAL.sub(section, self.template)
            body = PLACEHOLDER.sub(lambda match: present.get(match.group(1), ""), body)
        else:
            body = " ".join(present[option.name] for option in self.options
                            if option.type != "attachment" and option.name in present)
        body = " ".join(body.split())
        return f"${self.name}" + (f" {body}" if body else "")

    def usage(self) -> str:
        """`$name <option> [option]` for help texts."""
        parts = [f"<{option.name}>" if option.required else f"[{option.name}]"
                 for option in self.options if option.type != "attachment"]
        return " ".join([f"${self.name}", *parts])


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def derive(name: str, description: str, argument: str | None, parameters: Mapping[str, Mapping[str, Any]],
           required: tuple[str, ...]) -> Command:
    """The declaration for a plain `command="name"`: one option for `command_argument`, if any."""
    options = ()
    if argument:
        schema = dict(parameters.get(argument) or {})
        kind = {"integer": "integer", "number": "number", "boolean": "boolean"}.get(str(schema.get("type")), "string")
        options = (Option(argument, str(schema.get("description") or "")[:100], kind, argument in required,
                          tuple(schema.get("enum") or ())),)
    return Command(name, first_sentence(description), options)


def first_sentence(text: str, limit: int = 100) -> str:
    sentence = re.split(r"(?<=[.!?。！？])\s", text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"

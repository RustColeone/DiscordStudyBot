"""Extension declaration: one entry file, any of three contributions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from contracts.capability import Capability
from contracts.engine import Engine, Persona
from contracts.interface import Interface

NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ExtensionContext(Protocol):
    """What an extension receives at setup."""

    name: str
    config: Mapping[str, Any]
    data_dir: Path

    def log(self, event: str, **fields: Any) -> None: ...


@dataclass(frozen=True)
class Contributions:
    """What an extension adds. Besides interfaces, engines, capabilities and personas, an extension
    may suggest defaults so it works when dropped in: model `profiles` (name → ordered candidates, as
    in core.yml), and `assignments` and `admission` rules for its own interfaces' addresses
    (`<interface>:*` or `<interface>:<conversation>`). core.yml and runtime edits always win."""

    interfaces: Sequence[Interface] = ()
    engines: Sequence[Engine] = ()
    capabilities: Sequence[Capability] = ()
    personas: Sequence[Persona] = ()
    profiles: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    assignments: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    admission: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class Setting:
    """One setting a module asks for, so `python -m core setup` and `$setup` can offer it.

    `key` is the key in the module's `config.yml`. A `secret` setting (a token, an API key) is never kept
    there nor shown in chat: it goes into `.env` under `env` (the first name if several; any of them
    counts as set), and only the guided setup on the machine asks for it. `ask` puts it in the first-run
    questions. `check`, if given, tries a newly entered value (`async check(value) -> str`): it returns a
    short success note (for example the bot's name) or raises ValueError with the reason.
    """

    key: str
    description: str
    kind: str = "text"            # text, secret, number, boolean, list, path
    default: Any = None
    env: str | tuple[str, ...] = ()
    ask: bool = False
    check: Callable[[str], Awaitable[str]] | None = None

    KINDS = ("text", "secret", "number", "boolean", "list", "path")

    def __post_init__(self):
        if self.kind not in self.KINDS:
            raise ValueError(f"Setting {self.key!r}: kind must be one of {self.KINDS}")
        names = (self.env,) if isinstance(self.env, str) else tuple(self.env)
        object.__setattr__(self, "env", tuple(name for name in names if name))
        if self.kind == "secret" and not self.env:
            raise ValueError(f"Setting {self.key!r}: a secret needs `env` (the .env variable it is kept in)")
        if not re.match(r"^[a-z][a-z0-9_]*$", self.key):
            raise ValueError(f"Setting keys are lowercase letters, digits or _, got {self.key!r}")


@dataclass(frozen=True)
class Extension:
    """Declared as `EXTENSION` in an extension's `extension.py`.

    `setup` receives an ExtensionContext and returns Contributions. `contracts` is the major
    contracts version the extension was written for. `settings` declares what the module can be set
    up with (see Setting); it is read without running `setup`.
    """

    name: str
    setup: Callable[[ExtensionContext], Contributions]
    contracts: str = "1"
    version: str = "0"
    description: str = ""
    settings: Sequence[Setting] = ()

    def __post_init__(self):
        if not NAME.match(self.name):
            raise ValueError(f"Extension name must be lowercase letters, digits or _, got {self.name!r}")
        object.__setattr__(self, "settings", tuple(self.settings))
        keys = [setting.key for setting in self.settings]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.name}: setting keys must be unique")

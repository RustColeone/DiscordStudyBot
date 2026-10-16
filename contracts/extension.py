"""Extension declaration: one entry file, any of three contributions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

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
class Extension:
    """Declared as `EXTENSION` in an extension's `extension.py`.

    `setup` receives an ExtensionContext and returns Contributions. `contracts` is the major
    contracts version the extension was written for.
    """

    name: str
    setup: Callable[[ExtensionContext], Contributions]
    contracts: str = "1"
    version: str = "0"
    description: str = ""

    def __post_init__(self):
        if not NAME.match(self.name):
            raise ValueError(f"Extension name must be lowercase letters, digits or _, got {self.name!r}")

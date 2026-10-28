"""Extension declaration: one entry file, any of three contributions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from contracts.capability import Capability
from contracts.engine import Engine, Persona, PromptPart
from contracts.interface import Interface
from contracts.models import AdapterFactory
from contracts.stores import Facts
from contracts.watchers import Watcher

NAME = re.compile(r"^[a-z][a-z0-9_]*$")
PACKAGE = re.compile(r"^package:[A-Za-z0-9][A-Za-z0-9._-]*(>=[0-9][0-9A-Za-z.]*)?$")


class ExtensionContext(Protocol):
    """What an extension receives at setup."""

    name: str
    config: Mapping[str, Any]
    data_dir: Path
    facts: Facts  # added in 1.9: what this module publishes about personas, for interfaces (any time after setup)
    present: frozenset  # added in 1.10: which of the modules it `uses` are loaded (they were set up before it)
    prices: Any  # added in 1.12: the core's model prices (contracts.Prices); None when loaded without a core

    def log(self, event: str, **fields: Any) -> None: ...

    def personas(self) -> list[dict]:
        """Added in 1.11: the agents loaded so far (the core's and those of the modules set up before this one, so
        every module it requires or uses that is there), as `InterfacePort.personas` describes them, each with the
        `module` that added her, her `description`, `profile`, `profiles` and `models` (`default` and each tier → the
        candidates, `{provider, model}`). Called any time after setup, it lists them all."""


@dataclass(frozen=True)
class Contributions:
    """What an extension adds. Besides interfaces, engines, capabilities and personas, an extension
    may suggest defaults so it works when dropped in: model `providers` (name → settings, as in core.yml;
    configured settings win key by key) and `profiles` (name → ordered candidates, as in core.yml), and
    `assignments` and `admission` rules for its own interfaces' addresses (`<interface>:*` or
    `<interface>:<conversation>`). core.yml and runtime edits always win.

    `permissions` suggests permission defaults in the core.yml `permissions:` form
    (`{pattern: {use, tiers, rules}}`): `use` and `tiers` only for its own interfaces' addresses, `rules`
    only for its own capabilities. The module's config.yml `permissions:` overrides these, and core.yml
    overrides both, setting by setting.

    `adapters` adds provider kinds (kind → AdapterFactory) for APIs the built-in kinds do not speak; its
    providers, and core.yml's, may then use `kind: <that kind>`. A kind that already exists is refused.

    `watchers` (added in 1.9) see every persona's delivered messages, whichever engine she spoke in (see Watcher)."""

    interfaces: Sequence[Interface] = ()
    engines: Sequence[Engine] = ()
    capabilities: Sequence[Capability] = ()
    personas: Sequence[Persona] = ()
    profiles: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    assignments: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    admission: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    permissions: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # Added later, so they go last: plugins may pass the fields above positionally.
    providers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    adapters: Mapping[str, AdapterFactory] = field(default_factory=dict)
    prompt_parts: Sequence[PromptPart] = ()  # parts for every persona, or for those each part names
    watchers: Sequence[Watcher] = ()


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

    `requires` and `uses` (added in 1.10) declare the other modules it builds on, by module name. Without one it
    `requires`, the module is not loaded (and says why); a Python package is `package:<name>` or
    `package:<name>>=<version>`. Without one it `uses`, it loads, minus what that module would add: its setup is
    told which are there (`ExtensionContext.present`). Every engine and persona its defaults name must belong to the
    core or to a module it declares, or it is refused.

    `formerly` (added in 1.11): module names it had before, or modules it took in. Its data folder under an old name
    becomes its own (when it has none yet), its settings under an old name (`extension_config.<old>` in core.yml, and
    `config.yml` in a folder of that name beside its own) are read under its own, its stores and facts move to it,
    and another module declaring an old name is given this one.
    """

    name: str
    setup: Callable[[ExtensionContext], Contributions]
    contracts: str = "1"
    version: str = "0"
    description: str = ""
    settings: Sequence[Setting] = ()
    requires: Sequence[str] = ()
    uses: Sequence[str] = ()
    formerly: Sequence[str] = ()

    def __post_init__(self):
        if not NAME.match(self.name):
            raise ValueError(f"Extension name must be lowercase letters, digits or _, got {self.name!r}")
        object.__setattr__(self, "settings", tuple(self.settings))
        for key in ("requires", "uses"):
            value = getattr(self, key)
            if isinstance(value, str):
                raise ValueError(f"{self.name}: `{key}` is a list of module names, not one string")
            value = tuple(value)
            for item in value:
                if not (NAME.match(item) or (key == "requires" and PACKAGE.match(item))):
                    raise ValueError(f"{self.name}: `{key}` takes module names"
                                     + (" or package:<name>" if key == "requires" else "") + f", not {item!r}")
                if item == self.name:
                    raise ValueError(f"{self.name} cannot depend on itself")
            object.__setattr__(self, key, value)
        formerly = (self.formerly,) if isinstance(self.formerly, str) else tuple(self.formerly)
        if not all(isinstance(item, str) and NAME.match(item) and item != self.name for item in formerly):
            raise ValueError(f"{self.name}: `formerly` takes other module names")
        object.__setattr__(self, "formerly", formerly)
        both = set(self.requires) & set(self.uses)
        if both:
            raise ValueError(f"{self.name}: {sorted(both)} cannot be both required and used")
        keys = [setting.key for setting in self.settings]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.name}: setting keys must be unique")

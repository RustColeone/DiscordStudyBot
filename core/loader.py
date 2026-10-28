"""Loads extensions from directories. Each directory has an `extension.py` declaring `EXTENSION`.

The directory is imported as a private package, so the extension may use relative imports and may
live in any repository. The core never imports extension code any other way.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import importlib.util
import inspect
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

from contracts import (CONTRACTS_VERSION, Capability, Contributions, Engine, Extension, Interface, Persona, PromptPart,
                       Watcher)


class ExtensionError(RuntimeError):
    pass


@dataclass
class ExtensionContextImpl:
    name: str
    config: Mapping[str, Any]
    data_dir: Path
    _log: Callable[..., None]
    facts: Any = None  # contracts.Facts for this module (contracts 1.9); None when loaded without a core
    present: frozenset = frozenset()  # the modules it `uses` that are loaded (contracts 1.10)
    _personas: Callable[[], list] | None = None  # the agents loaded so far (contracts 1.11)

    def log(self, event: str, **fields: Any) -> None:
        self._log("extension", extension=self.name, detail=event, **fields)

    def personas(self) -> list[dict]:
        return list(self._personas()) if self._personas is not None else []


@dataclass
class LoadedExtension:
    extension: Extension
    path: Path
    module: ModuleType
    contributions: Contributions
    config: Mapping[str, Any] = None  # the settings it was set up with (its config.yml, then core.yml's)


def _package_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    return "botext_" + re.sub(r"\W", "_", path.name) + "_" + digest


def import_extension(path: str | Path) -> tuple[Extension, ModuleType]:
    path = Path(path).resolve()
    entry = path / "extension.py"
    if not entry.is_file():
        raise ExtensionError(f"{path} has no extension.py")
    package = _package_name(path)
    if package not in sys.modules:
        init = path / "__init__.py"
        if init.is_file():
            spec = importlib.util.spec_from_file_location(package, init, submodule_search_locations=[str(path)])
        else:
            spec = importlib.machinery.ModuleSpec(package, None, is_package=True)
            spec.submodule_search_locations = [str(path)]
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        if init.is_file():
            try:
                spec.loader.exec_module(module)
            except Exception as error:
                sys.modules.pop(package, None)
                raise ExtensionError(f"Importing {init} failed: {type(error).__name__}: {error}") from error
    try:
        module = importlib.import_module(package + ".extension")
    except Exception as error:
        raise ExtensionError(f"Importing {entry} failed: {type(error).__name__}: {error}") from error
    extension = getattr(module, "EXTENSION", None)
    if not isinstance(extension, Extension):
        raise ExtensionError(f"{entry} must define EXTENSION = contracts.Extension(...)")
    if str(extension.contracts).split(".")[0] != CONTRACTS_VERSION.split(".")[0]:
        raise ExtensionError(f"{extension.name} targets contracts {extension.contracts}, core has {CONTRACTS_VERSION}")
    return extension, module


def local_config(path: str | Path) -> dict:
    """The extension folder's own `config.yml` (kept out of git), or {}."""
    file = Path(path) / "config.yml"
    if not file.is_file():
        return {}
    import yaml
    value = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ExtensionError(f"{file} must be a mapping")
    return value


def adopt_data(data_root: Path, extension: Extension, log: Callable[..., None]) -> None:
    """Extension.formerly: the data folder under an old name becomes the module's own, when it has none yet (or an
    empty one); one with data of its own is left, and so is the old one."""
    data_dir = data_root / extension.name
    for old in extension.formerly:
        before = data_root / old
        if not before.is_dir():
            continue
        if data_dir.is_dir() and any(data_dir.iterdir()):
            log("module_data_kept", extension=extension.name, old=old, path=str(before),
                hint="both folders hold data; the old one is not read (merge them by hand)")
            continue
        if data_dir.is_dir():
            data_dir.rmdir()
        shutil.move(str(before), str(data_dir))
        log("module_data_moved", extension=extension.name, old=old, path=str(data_dir))


def settings(path: Path, extension: Extension, config_for: Callable[[str], Mapping[str, Any]],
             log: Callable[..., None]) -> dict:
    """The module's settings: its folder's config.yml, then core.yml's `extension_config`. Under each, those written
    for a name it had before (Extension.formerly: the config.yml of a folder of that name beside its own, and
    `extension_config.<old>`), which its own win over."""
    from core.defaults import merge
    config: dict = {}
    for old in extension.formerly:
        written, configured = local_config(path.parent / old), dict(config_for(old) or {})
        if written or configured:
            log("module_settings_moved", extension=extension.name, old=old, keys=sorted({*written, *configured}),
                hint=f"settings written for {old} are read as {extension.name}'s (move them)")
        config = merge(config, written)
    config = merge(config, local_config(path))
    for old in extension.formerly:
        config = merge(config, dict(config_for(old) or {}))
    return merge(config, dict(config_for(extension.name) or {}))


async def load_extension(path: str | Path, config_for: Callable[[str], Mapping[str, Any]], data_root: Path,
                         log: Callable[..., None], facts_for: Callable[[str], Any] | None = None,
                         present: frozenset = frozenset(), imported: tuple[Extension, ModuleType] | None = None,
                         personas: Callable[[], list] | None = None) -> LoadedExtension:
    """Imports the module (unless `imported` already has it) and runs its setup; `present` is which of the modules
    it uses are loaded, `personas` lists the agents loaded so far."""
    extension, module = imported or import_extension(path)
    adopt_data(data_root, extension, log)
    data_dir = data_root / extension.name
    data_dir.mkdir(parents=True, exist_ok=True)
    config = settings(Path(path).resolve(), extension, config_for, log)
    context = ExtensionContextImpl(extension.name, config, data_dir, log,
                                   facts_for(extension.name) if facts_for is not None else None, frozenset(present),
                                   personas)
    try:
        contributions = extension.setup(context)
        if inspect.isawaitable(contributions):
            contributions = await contributions
    except Exception as error:
        raise ExtensionError(f"{extension.name} setup failed: {type(error).__name__}: {error}") from error
    if not isinstance(contributions, Contributions):
        raise ExtensionError(f"{extension.name} setup must return contracts.Contributions")
    for item in contributions.interfaces:
        if not isinstance(item, Interface):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.Interface")
    for item in contributions.engines:
        if not isinstance(item, Engine):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.Engine")
    for item in contributions.capabilities:
        if not isinstance(item, Capability):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.Capability")
    for item in contributions.personas:
        if not isinstance(item, Persona):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.Persona")
    if not isinstance(contributions.prompt_parts, (list, tuple)):
        raise ExtensionError(f"{extension.name}: prompt_parts is a list of contracts.PromptPart")
    for item in contributions.prompt_parts:
        if not isinstance(item, PromptPart):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.PromptPart")
    if not isinstance(contributions.watchers, (list, tuple)):
        raise ExtensionError(f"{extension.name}: watchers is a list of contracts.Watcher")
    for item in contributions.watchers:
        if not isinstance(item, Watcher):
            raise ExtensionError(f"{extension.name}: {item!r} is not a contracts.Watcher")
    return LoadedExtension(extension, Path(path).resolve(), module, contributions, config)

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
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

from contracts import CONTRACTS_VERSION, Capability, Contributions, Engine, Extension, Interface, Persona


class ExtensionError(RuntimeError):
    pass


@dataclass
class ExtensionContextImpl:
    name: str
    config: Mapping[str, Any]
    data_dir: Path
    _log: Callable[..., None]

    def log(self, event: str, **fields: Any) -> None:
        self._log("extension", extension=self.name, detail=event, **fields)


@dataclass
class LoadedExtension:
    extension: Extension
    path: Path
    module: ModuleType
    contributions: Contributions


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


async def load_extension(path: str | Path, config_for: Callable[[str], Mapping[str, Any]], data_root: Path,
                         log: Callable[..., None]) -> LoadedExtension:
    extension, module = import_extension(path)
    data_dir = data_root / extension.name
    data_dir.mkdir(parents=True, exist_ok=True)
    from core.defaults import merge
    config = merge(local_config(path), dict(config_for(extension.name) or {}))
    context = ExtensionContextImpl(extension.name, config, data_dir, log)
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
    return LoadedExtension(extension, Path(path).resolve(), module, contributions)

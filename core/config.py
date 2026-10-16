"""Core configuration (YAML) and optional .env loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from contracts import Address, Persona
from core.manager.policy import Assignment, Owners


class ConfigError(ValueError):
    pass


def load_env_file(path: Path) -> None:
    """Set variables from a KEY=VALUE file without overriding the real environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


class CoreConfig:
    def __init__(self, raw: dict[str, Any], base_dir: Path):
        self.raw, self.base_dir = raw, base_dir
        self.data_dir = self.path(raw.get("data_dir", "data/core"))
        self.extensions = [self.path(item) for item in raw.get("extensions", [])]
        self.plugin_dirs = [self.path(item) for item in raw.get("plugin_dirs", [])]
        self.extension_config = dict(raw.get("extension_config") or {})
        self.providers = dict(raw.get("providers") or {})
        self.profiles = dict(raw.get("profiles") or {})
        self.owners = Owners(raw.get("owners"))
        self.control = Address.parse(raw["control"]) if raw.get("control") else None
        self.rules = dict(raw.get("admission") or {})
        self.routes = dict(raw.get("routes") or {})
        self.routines = dict(raw.get("routines") or {})
        self.audit_echo = bool(raw.get("audit_echo", False))
        try:
            self.personas = {name: Persona(name, value.get("display_name", name), value["prompt"],
                                           value.get("profile", "default"))
                             for name, value in (raw.get("personas") or {}).items()}
            self.assignments = {pattern: Assignment.from_config(pattern, value)
                                for pattern, value in (raw.get("assignments") or {}).items()}
        except (KeyError, TypeError) as error:
            raise ConfigError(f"Invalid persona or assignment: {error}") from error

    def plugins(self) -> list[Path]:
        """Drop-in extensions: every folder with an extension.py in a plugin directory, not listed already."""
        listed = {path.resolve() for path in self.extensions}
        found = []
        for directory in self.plugin_dirs:
            if directory.is_dir():
                found.extend(path for path in sorted(directory.iterdir())
                             if (path / "extension.py").is_file() and path.resolve() not in listed)
        return found

    def path(self, value: str) -> Path:
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (self.base_dir / path).resolve()

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CoreConfig":
        """Built-in defaults, with `path` (if it exists) merged over them. Env files load first."""
        from core.defaults import ROOT, default_config, merge
        raw: dict = {}
        base = ROOT
        if path is not None and Path(path).is_file():
            path = Path(path).resolve()
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ConfigError("The core configuration must be a mapping")
            base = path.parent
        for env_file in raw.get("env_files", [".env"]):
            path_value = Path(os.path.expanduser(str(env_file)))
            load_env_file(path_value if path_value.is_absolute() else (base / path_value))
        if base != ROOT and "env_files" not in raw:
            load_env_file(ROOT / ".env")
        defaults = default_config()
        # Paths written in core.yml are relative to core.yml; built-in ones are already absolute.
        for key in ("data_dir",):
            if key in raw:
                raw[key] = str(cls._resolve(raw[key], base))
        for key in ("extensions", "plugin_dirs"):
            if key in raw:
                raw[key] = [str(cls._resolve(item, base)) for item in raw[key]]
        return cls(merge(defaults, raw), base)

    @staticmethod
    def _resolve(value, base: Path) -> Path:
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (base / path).resolve()

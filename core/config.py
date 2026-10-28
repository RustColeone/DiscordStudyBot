"""Core configuration (YAML) and optional .env loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from contracts import Address, Persona
from core.manager.policy import Assignment, Owners, Permissions
from core.prompts import parts_from_config


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
    """`modules` decides what loads: every module folder (one with an `extension.py`) in `modules.dirs`
    (default: `extensions/`, the built-in modules, then `plugins/`, external ones), plus the folders
    in `modules.paths` (external), except those in `modules.exclude` (a folder name such as `terminal`,
    or a path ending such as `extensions/terminal`). Excluded modules are not imported."""

    MODULE_KEYS = frozenset({"dirs", "paths", "exclude"})

    def __init__(self, raw: dict[str, Any], base_dir: Path):
        self.raw, self.base_dir = raw, base_dir
        # Where setup writes: the core.yml this came from (None: built-in defaults only) and the .env file.
        self.file: Path | None = None
        self.env_file: Path = base_dir / ".env"
        self.env_files: list[Path] = [base_dir / ".env"]
        for legacy in ("extensions", "plugin_dirs"):
            if legacy in raw:
                raise ConfigError(f"`{legacy}:` is no longer used: every module in extensions/ and plugins/ loads. "
                                  "Remove it; list modules not to load under `modules: {exclude: [...]}` and folders "
                                  "elsewhere under `modules: {paths: [...]}`.")
        modules = raw.get("modules") or {}
        if not isinstance(modules, dict) or set(modules) - self.MODULE_KEYS:
            raise ConfigError(f"`modules:` takes only {sorted(self.MODULE_KEYS)}")
        self.data_dir = self.path(raw.get("data_dir", "data/core"))
        self.module_dirs = [self.path(item) for item in modules.get("dirs", [])]
        self.module_paths = [self.path(item) for item in modules.get("paths", [])]
        self.exclude = [str(item).strip().strip("/") for item in modules.get("exclude", []) if str(item).strip()]
        self.extension_config = dict(raw.get("extension_config") or {})
        self.providers = dict(raw.get("providers") or {})
        self.configured_providers = self.providers  # what the owner wrote: a module's suggestions never override it
        self.profiles = dict(raw.get("profiles") or {})
        self.owners = Owners(raw.get("owners"))
        self.control = Address.parse(raw["control"]) if raw.get("control") else None
        self.rules = dict(raw.get("admission") or {})
        self.routes = dict(raw.get("routes") or {})
        self.routines = dict(raw.get("routines") or {})
        self.audit_echo = bool(raw.get("audit_echo", False))
        secrets = raw.get("secrets") or []
        if isinstance(secrets, str) or not all(isinstance(item, str) for item in secrets):
            raise ConfigError("`secrets:` must be a list of file paths or patterns")
        self.secret_paths = list(secrets)
        # A persona entry with a `prompt` is a persona; one without adds its `parts` to a module's persona.
        self.personas: dict[str, Persona] = {}
        self.persona_parts: dict[str, tuple] = {}
        try:
            for name, value in (raw.get("personas") or {}).items():
                if not isinstance(value, dict):
                    raise ConfigError(f"persona {name!r} is a mapping (prompt, display_name, profile, parts)")
                parts = parts_from_config(name, value.get("parts"), self.path)
                if "prompt" in value:
                    self.personas[name] = Persona(name, value.get("display_name", name), value["prompt"],
                                                  value.get("profile", "default"), parts)
                elif set(value) - {"parts"}:
                    raise ConfigError(f"persona {name!r} has no `prompt`: such an entry only adds `parts` to a "
                                      f"module's persona; to change {sorted(set(value) - {'parts'})}, give the "
                                      "whole persona, prompt included")
                else:
                    self.persona_parts[name] = parts
            self.assignments = {pattern: Assignment.from_config(pattern, value)
                                for pattern, value in (raw.get("assignments") or {}).items()}
            # The core's own defaults, where core.yml left them as they are: they apply only while what they name is
            # loaded, while an assignment the owner wrote that names something missing stops the core.
            from core.defaults import default_config
            builtin = default_config().get("assignments") or {}
            self.builtin_assignments = [pattern for pattern, value in builtin.items()
                                        if (raw.get("assignments") or {}).get(pattern) == value]
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"Invalid persona or assignment: {error}") from error
        self.permissions = dict(raw.get("permissions") or {})
        self.builtin_permissions: dict = {}  # set by load(): the built-in defaults, a layer of their own
        try:
            Permissions([("core.yml", self.permissions)])
        except ValueError as error:
            raise ConfigError(str(error)) from error

    def all_modules(self) -> list[Path]:
        """Every module folder, excluded ones too, built-in ones first, each once."""
        candidates = []
        for directory in self.module_dirs:
            if directory.is_dir():
                candidates.extend(path for path in sorted(directory.iterdir()) if (path / "extension.py").is_file())
        candidates.extend(self.module_paths)
        found, seen = [], set()
        for path in candidates:
            if path.resolve() not in seen:
                seen.add(path.resolve())
                found.append(path)
        return found

    def modules(self) -> list[Path]:
        """The module folders to load: all of them except the excluded ones."""
        return [path for path in self.all_modules() if not self.excluded(path)]

    def excluded(self, path: Path) -> bool:
        parts = path.resolve().parts
        for entry in self.exclude:
            wanted = Path(entry).parts
            if parts[-len(wanted):] == wanted or (len(wanted) > 1 and path.resolve() == self.path(entry)):
                return True
        return False

    @staticmethod
    def builtin(path: Path) -> bool:
        """Built-in modules are the ones shipped in this repository's `extensions/`."""
        from core.defaults import ROOT
        return path.resolve().parent == ROOT / "extensions"

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
        env_files = [cls._resolve(item, base) for item in raw.get("env_files", [".env"])]
        for env_file in env_files:
            load_env_file(env_file)
        loaded = list(env_files)
        if base != ROOT and "env_files" not in raw:
            load_env_file(ROOT / ".env")
            loaded.append(ROOT / ".env")
        defaults = default_config()
        # Paths written in core.yml are relative to core.yml; built-in ones are already absolute.
        for key in ("data_dir",):
            if key in raw:
                raw[key] = str(cls._resolve(raw[key], base))
        modules = raw.get("modules")
        if isinstance(modules, dict):
            for key in ("dirs", "paths"):
                if key in modules:
                    modules[key] = [str(cls._resolve(item, base)) for item in modules[key]]
        config = cls(merge(defaults, raw), base)
        config.file = path if path is not None and Path(path).is_file() else None
        config.env_file = env_files[0] if env_files else base / ".env"
        config.env_files = loaded
        # Permissions layer setting by setting (built-in, then modules, then core.yml), so keep them apart.
        config.builtin_permissions = dict(defaults.get("permissions") or {})
        config.permissions = dict(raw.get("permissions") or {})
        # Providers likewise: a module's suggestions override the built-in settings, core.yml overrides both.
        config.configured_providers = dict(raw.get("providers") or {})
        return config

    @staticmethod
    def _resolve(value, base: Path) -> Path:
        path = Path(os.path.expanduser(str(value)))
        return path if path.is_absolute() else (base / path).resolve()

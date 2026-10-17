"""Setting the bot up: what each module can be set to, and writing the answers where they belong.

Shared by the guided setup (`python -m core setup`, core/wizard.py) and `$setup` in a chat. Which modules
load goes into core.yml (`modules.exclude`), a module's own settings into its folder's config.yml (git-
ignored), secrets into .env. Files are edited one top-level key at a time: everything else in them,
comments included, stays exactly as it was.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from contracts import Extension, Setting
from core.config import CoreConfig
from core.defaults import ROOT
from core.loader import ExtensionError, import_extension, local_config

TRUE = {"yes", "y", "true", "on", "1"}
FALSE = {"no", "n", "false", "off", "0"}
MENTION = re.compile(r"^<[#@!&]+(\d+)>$")


# ---- file edits -------------------------------------------------------------------------------------
def _block(lines: list[str], key: str) -> tuple[int, int] | None:
    """Line range of a top-level `key:` and everything under it: indented lines, and list items written at
    column 0 (`- item`, as YAML usually writes a top-level list)."""
    start = next((index for index, line in enumerate(lines) if re.match(rf"^{re.escape(key)}\s*:", line)), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t" or lines[end] == "-"
                                or lines[end].startswith("- ")
                                or (lines[end].startswith("#") and end + 1 < len(lines)
                                    and lines[end + 1][:1] in (" ", "\t"))):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def set_key(path: Path, key: str, value: Any, header: str = "") -> None:
    """Set (or with `value=None` remove) one top-level key of a YAML mapping file, keeping the rest as is."""
    text = path.read_text(encoding="utf-8") if path.exists() else header
    current = yaml.safe_load(text) or {}
    if not isinstance(current, dict):
        raise ValueError(f"{path} is not a YAML mapping")
    lines = text.splitlines()
    found = _block(lines, key)
    new = [] if value is None else yaml.safe_dump({key: value}, allow_unicode=True, sort_keys=False,
                                                  default_flow_style=False, width=4096).splitlines()
    if found:
        lines[found[0]:found[1]] = new
    else:
        # A new key goes right after the file's opening comments, where people look first.
        at = next((index for index, line in enumerate(lines) if line.strip() and not line.startswith("#")),
                  len(lines))
        lines[at:at] = new
    result = "\n".join(lines).rstrip("\n") + "\n"
    check = yaml.safe_load(result) or {}
    expected = {**current, key: value} if value is not None else {k: v for k, v in current.items() if k != key}
    if check != expected:
        raise ValueError(f"Could not edit {path} safely; change `{key}` there by hand")
    created = not path.exists()
    path.write_text(result, encoding="utf-8")
    if created:
        path.chmod(0o600)


def set_env(path: Path, name: str, value: str) -> None:
    """Set `name=value` in a .env file (other lines kept), readable only by this user."""
    if "\n" in value or "\r" in value:
        raise ValueError("A secret is one line")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(name)}\s*=")
    entry = f'{name}="{value}"' if re.search(r"[\s#]", value) else f"{name}={value}"
    out, done = [], False
    for line in lines:
        if pattern.match(line):
            if not done:
                out.append(entry)
                done = True
            continue
        out.append(line)
    if not done:
        out.append(entry)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.chmod(0o600)
    os.environ[name] = value


# ---- values -----------------------------------------------------------------------------------------
def parse(setting: Setting, text: str) -> Any:
    text = text.strip()
    if setting.kind == "boolean":
        if text.lower() in TRUE:
            return True
        if text.lower() in FALSE:
            return False
        raise ValueError("Answer yes or no")
    if setting.kind == "number":
        try:
            number = float(text)
        except ValueError:
            raise ValueError("Enter a number")
        return int(number) if number.is_integer() else number
    if setting.kind == "list":
        if text.lower() in ("", "none", "[]", "-"):
            return []
        items = [item for item in re.split(r"[\s,]+", text) if item]
        return [MENTION.sub(r"\1", item) for item in items]  # a pasted <#channel> mention becomes its ID
    if "\n" in text:
        raise ValueError("One line only")
    return text


def show(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


# ---- modules ----------------------------------------------------------------------------------------
@dataclass
class Module:
    path: Path
    builtin: bool
    excluded: bool
    extension: Extension | None
    error: str = ""

    @property
    def name(self) -> str:
        return self.extension.name if self.extension else self.path.name

    @property
    def settings(self) -> tuple[Setting, ...]:
        return tuple(self.extension.settings) if self.extension else ()

    @property
    def description(self) -> str:
        if self.extension:
            return self.extension.description
        return "(switched off; not loaded)" if self.excluded else f"(cannot be read: {self.error})"

    def setting(self, key: str) -> Setting | None:
        return next((item for item in self.settings if item.key == key), None)


class Setup:
    def __init__(self, config: CoreConfig):
        self.config = config
        self.file = config.file or Path("core.yml").resolve()

    def modules(self) -> list[Module]:
        """Every module folder. Switched-off ones are listed by folder only and never imported: their code
        does not run until they are switched on (then their settings show)."""
        found = []
        for path in self.config.all_modules():
            excluded = self.config.excluded(path)
            extension, error = None, ""
            if not excluded:
                try:
                    extension = import_extension(path)[0]
                except ExtensionError as failure:
                    error = str(failure)
            found.append(Module(path, self.config.builtin(path), excluded, extension, error))
        return found

    def module(self, name: str) -> Module | None:
        name = name.strip().strip("/")
        return next((item for item in self.modules() if name in (item.name, item.path.name)
                     or str(item.path).endswith("/" + name)), None)

    # ---- reading ----
    def value(self, module: Module, setting: Setting) -> tuple[Any, str]:
        """The setting's value and where it comes from: core.yml, config.yml, .env or default."""
        if setting.kind == "secret":
            if any(os.environ.get(name, "").strip() for name in setting.env):
                return True, ".env"
            return bool(local_config(module.path).get(setting.key)), "config.yml"
        override = self.config.extension_config.get(module.name) or {}
        if setting.key in override:
            return override[setting.key], "core.yml"
        local = local_config(module.path)
        if setting.key in local:
            return local[setting.key], "config.yml"
        return setting.default, "default"

    def missing(self, module: Module) -> list[Setting]:
        """First-run settings (`ask`) the module still needs."""
        return [item for item in module.settings if item.ask and not self.value(module, item)[0]]

    # ---- writing ----
    def write(self, module: Module, setting: Setting, value: Any) -> Path:
        """Save a value (None: back to the default). Secrets go to .env, the rest to the module's config.yml."""
        if setting.kind == "secret":
            if value is None:
                raise ValueError("A secret cannot be reset here; remove it from .env")
            set_env(self.config.env_file, setting.env[0], str(value).strip())
            return self.config.env_file
        path = module.path / "config.yml"
        set_key(path, setting.key, value,
                header=f"# {module.name} settings for this machine (git-ignored); see config.example.yml.\n")
        return path

    def entry(self, module: Module) -> str:
        """How core.yml names a module to exclude: its folder under extensions/ or plugins/, else its name."""
        for folder in ("extensions", "plugins"):
            if module.path.resolve().parent == ROOT / folder:
                return f"{folder}/{module.path.name}"
        return module.path.name

    def enable(self, module: Module, on: bool) -> Path:
        current = list(self.config.exclude)
        if on:
            keep = [item for item in current if not self._matches(module.path, item)]
        else:
            keep = current if self.config.excluded(module.path) else current + [self.entry(module)]
        if keep != current:
            raw = yaml.safe_load(self.file.read_text(encoding="utf-8")) if self.file.exists() else {}
            modules = dict((raw or {}).get("modules") or {})
            if keep:
                modules["exclude"] = keep
            else:
                modules.pop("exclude", None)
            set_key(self.file, "modules", modules or None,
                    header="# Shared settings (git-ignored); see core.example.yml. Written by setup.\n")
            self.config.exclude = keep
        return self.file

    @staticmethod
    def _matches(path: Path, entry: str) -> bool:
        wanted = Path(entry).parts
        return path.resolve().parts[-len(wanted):] == wanted


RESTART = "Restart the bot for this to take effect."


async def command(setup: Setup, text: str, interface: str, markdown: bool = True) -> str:
    """`$setup`: modules and their settings, as the creator sees them in a chat. Secrets are never shown and
    never accepted here: only the guided setup on the machine (`python -m core setup`) takes them."""
    code = (lambda value: f"`{value}`") if markdown else str
    words = text.split()
    if not words:
        lines = ["Modules (all load unless switched off):"]
        for module in setup.modules():
            needs = setup.missing(module) if not module.excluded else []
            lines.append(f"{'○ off' if module.excluded else '● on '} {code(module.name)} · "
                         f"{'built-in' if module.builtin else 'external'} · {module.description}"
                         + (f" · needs {', '.join(item.key for item in needs)}" if needs else ""))
        lines.append(f"{code('$setup <module>')} shows its settings · {code('$setup on|off <module>')} switches it.")
        return "\n".join(lines)
    if words[0].lower() in ("on", "off") and len(words) == 2:
        module = setup.module(words[1])
        if module is None:
            return f"No module {code(words[1])}. {code('$setup')} lists them."
        if words[0].lower() == "off" and interface in (module.name, module.path.name):
            return f"{module.name} is how you are talking to the bot; switch it off on the machine (python -m core setup)."
        if module.excluded == (words[0].lower() == "off"):
            return f"{module.name} is already {words[0].lower()}."
        path = setup.enable(module, words[0].lower() == "on")
        return f"{module.name} is switched {words[0].lower()} ({path.name}). {RESTART}"
    module = setup.module(words[0])
    if module is None:
        return f"No module {code(words[0])}. {code('$setup')} lists them."
    if len(words) == 1:
        if module.excluded:
            return f"{module.name} is off, so it is not loaded; {code(f'$setup on {module.name}')} and a restart first."
        if not module.settings:
            return f"{module.name} has no settings."
        lines = [f"{module.name} ({'off' if module.excluded else 'on'}):"]
        for setting in module.settings:
            value, source = setup.value(module, setting)
            shown = ("set" if value else "not set (on the machine: python -m core setup)") \
                if setting.kind == "secret" else f"{show(value)}" + (f" ({source})" if source != "default" else "")
            lines.append(f"{code(setting.key)} = {shown} · {setting.description}")
        lines.append(f"{code(f'$setup {module.name} <setting> <value>')} changes one ('default' resets it).")
        return "\n".join(lines)
    setting = module.setting(words[1])
    if setting is None:
        if module.excluded:
            return f"{module.name} is off, so it is not loaded; {code(f'$setup on {module.name}')} and a restart first."
        return f"{module.name} has no setting {code(words[1])}. {code(f'$setup {module.name}')} lists them."
    if setting.kind == "secret":
        return (f"{module.name}.{setting.key} is a secret: it is never set or shown in a chat. On the machine, run "
                "`python -m core setup`.")
    value, source = setup.value(module, setting)
    if len(words) == 2:
        return f"{module.name}.{setting.key} = {show(value)} ({source}) · {setting.description}"
    raw = text.split(None, 2)[2]
    try:
        new = None if raw.strip().lower() == "default" else parse(setting, raw)
        if new is not None and setting.check is not None:
            await setting.check(str(new))
        path = setup.write(module, setting, new)
    except (ValueError, OSError) as error:
        return f"Not saved: {error}."
    note = (f" Note: extension_config.{module.name}.{setting.key} in core.yml overrides it."
            if source == "core.yml" else "")
    return (f"{module.name}.{setting.key} = {show(setting.default if new is None else new)} "
            f"({path.parent.name}/{path.name}). {RESTART}{note}")

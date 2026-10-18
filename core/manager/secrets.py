"""Secrets enforcement (S1-S3): keep credentials out of models, chats and the bot's own records.

Scope: only what passes through the bot itself, meaning capability arguments, tool results, replies,
approval prompts and stored history. A process the bot starts (a terminal shell) reads files and the
network directly as the bot's OS user; that is bounded by the owner's approval of each line and by OS
isolation, never by this module.

* Known values: everything named in the env files, every `Setting(kind="secret")` variable, provider
  key variables, and any environment variable whose name ends in KEY, TOKEN, SECRET, PASSWORD,
  PASSPHRASE or CREDENTIAL(S) (values of 8+ characters).
* Formats: well-known key shapes (OpenAI/Anthropic `sk-…`, GitHub, Google, Slack, Discord bot tokens,
  private key blocks).
* Paths: `.env` files, private keys, SSH and cloud credentials, cookie stores, modules' config.yml,
  the core's databases, and `secrets:` from core.yml.
"""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from contracts import ToolResult

MIN_LENGTH = 8
# Names that end like a credential (DEEPSEEK_API_KEY, GITHUB_TOKEN), not ones that merely contain the word
# (GIT_CONFIG_KEY_0 holds a config key's name).
KEYISH = re.compile(r"(?:^|_)(?:API_?)?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?)$", re.I)
FORMATS = (
    ("api key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("Google key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}")),
    ("Slack token", re.compile(r"(?<![A-Za-z0-9])xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("Discord token", re.compile(r"(?<![A-Za-z0-9_\-])[MNO][A-Za-z\d_\-]{23,27}\.[A-Za-z\d_\-]{6}\.[A-Za-z\d_\-]{27,40}")),
    ("private key", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")),
)
DEFAULT_PATHS = (
    "*/.env", "*/.env.*", "*.pem", "*.key", "*/id_rsa*", "*/id_ed25519*", "*/id_ecdsa*", "~/.ssh/*",
    "*/cookies.sqlite", "~/.codex/*", "~/.netrc", "~/.git-credentials", "~/.aws/*", "~/.config/gh/*",
    "~/.docker/config.json", "/proc/*",
)


def env_names(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.append(line.split("=", 1)[0].strip().removeprefix("export ").strip())
    return [name for name in names if name]


def normalize(path: str) -> str:
    value = Path(os.path.expanduser(str(path).strip()))
    if not value.is_absolute():
        value = Path.home() / value
    return os.path.normpath(str(value))


class Secrets:
    def __init__(self, values: Mapping[str, str] | None = None, paths: Iterable[str] = ()):
        self.values: dict[str, str] = {}
        extra_paths = []
        for name, value in (values or {}).items():
            value = str(value or "").strip()
            if len(value) < MIN_LENGTH:
                continue
            if value.startswith(("/", "~")):
                extra_paths.append(value)  # a variable naming a credentials file: protect the file
                continue
            self.values[name] = value
        self._ordered = sorted(self.values.items(), key=lambda item: -len(item[1]))
        self.paths = [item if item.startswith("*") else normalize(item)
                      for item in (*DEFAULT_PATHS, *paths, *extra_paths)]

    @classmethod
    def collect(cls, *, env_files: Iterable[Path] = (), names: Iterable[str] = (), paths: Iterable[str] = (),
                environment: Mapping[str, str] | None = None) -> "Secrets":
        environment = os.environ if environment is None else environment
        wanted = set(names)
        for file in env_files:
            wanted.update(env_names(Path(file)))
        wanted.update(name for name in environment if KEYISH.search(name))
        values = {name: environment.get(name, "") for name in wanted}
        protected = [str(Path(file)) for file in env_files]
        return cls(values, [*protected, *paths])

    # ---- values -----------------------------------------------------------------------------------
    def _strings(self, value: Any) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, Mapping):
            for key, item in value.items():
                yield from self._strings(key)
                yield from self._strings(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                yield from self._strings(item)

    def find(self, value: Any) -> list[str]:
        """Names of the secrets found in a value (strings anywhere inside it)."""
        found: list[str] = []
        for text in self._strings(value):
            for name, secret in self._ordered:
                if secret in text and name not in found:
                    found.append(name)
            for label, pattern in FORMATS:
                if pattern.search(text) and label not in found:
                    found.append(label)
        return found

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for name, secret in self._ordered:
            if secret in text:
                text = text.replace(secret, f"[secret {name}]")
        for label, pattern in FORMATS:
            text = pattern.sub(f"[secret {label}]", text)
        return text

    def scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, Mapping):
            return {key: self.scrub_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub_value(item) for item in value)
        return value

    def scrub_result(self, result: ToolResult) -> ToolResult:
        summary = self.scrub(result.summary)
        data = self.scrub_value(dict(result.data))
        sources = tuple(self.scrub(item) for item in result.sources)
        if summary == result.summary and data == dict(result.data) and sources == result.sources:
            return result
        return dataclasses.replace(result, summary=summary, data=data, sources=sources)

    # ---- paths ------------------------------------------------------------------------------------
    def is_secret_path(self, path: str) -> bool:
        candidates = {normalize(path)}
        try:
            candidates.add(os.path.realpath(normalize(path)))
        except OSError:
            pass
        return any(fnmatch.fnmatchcase(candidate, pattern) for candidate in candidates for pattern in self.paths)

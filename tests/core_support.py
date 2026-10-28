"""Helpers for core tests: build a real Core in a temporary directory with fixture extensions."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from core.app import Core
from core.config import CoreConfig
from core.providers.adapters import AdapterResult

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "core_fixtures"


def _isolate_local_configs() -> None:
    """Tests never read a repository module's own `config.yml`: it is the owner's local, git-ignored settings
    (a web UI passphrase, a room's profiles), and tests must not depend on them. Tests of that file use
    module folders of their own, which are still read."""
    import core.loader
    import core.setup
    real = core.loader.local_config
    shared = (ROOT / "extensions", ROOT / "plugins")

    def local_config(path):
        folder = Path(path).resolve()
        return {} if any(folder.is_relative_to(root) for root in shared) else real(path)
    core.loader.local_config = core.setup.local_config = local_config


_isolate_local_configs()


class FakeAdapter:
    """A provider adapter returning scripted outputs (dicts become JSON text)."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        self.calls.append({"model": model, "messages": messages, "schema": schema, "effort": effort,
                           "images": list(images)})
        value = self.handler(messages, schema) if self.handler else self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AdapterResult):
            return value
        return AdapterResult(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), 10, 5)


class FakeToolAdapter(FakeAdapter):
    """A provider adapter that calls tools as functions: scripted answers, made with `calls(...)` (a string is a
    plain answer; a dict is JSON text, for requests without tools)."""

    supports_tools = True

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=(), tools=()):
        self.calls.append({"model": model, "messages": messages, "schema": schema, "effort": effort,
                           "images": list(images), "tools": list(tools)})
        value = self.handler(messages, tools) if self.handler else self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AdapterResult):
            return value
        return AdapterResult(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False), 10, 5)


CALL_IDS = iter(range(1, 10 ** 6))


def turn_context(call) -> str:
    """The part of a request that holds this turn's context (the time, notes, mood…), which the engines send after the
    history rather than in the system prompt."""
    for message in call["messages"]:
        content = str(message.get("content") or "")
        if "[This turn's context" in content:
            return content[content.index("[This turn's context"):]
    return ""


def calls(text="", *made, replay=None) -> AdapterResult:
    """A function-calling answer: its words, then each call as (tool name, arguments)."""
    from contracts.models import ToolCall
    return AdapterResult(text, 10, 5, schema_mode="tools", replay=replay,
                         tool_calls=tuple(ToolCall(f"call_{next(CALL_IDS)}", name, arguments) for name, arguments in made))


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result


def make_config(tmp: Path, overrides: dict | None = None) -> CoreConfig:
    raw = {
        "data_dir": str(tmp / "data"),
        "extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"), str(FIXTURES / "tools"),
                       str(ROOT / "extensions" / "basic"), str(ROOT / "extensions" / "assistant")],
        "extension_config": {
            "fake": {"interfaces": {"chat": {"quotes": True, "max_upload_mb": 1},
                                    "owner": {"progress": True}}},
            "tools": {"counter": str(tmp / "counter.json")},
            # The students' memory learns after each message with model calls of its own: off unless a test turns it
            # on (their scripted answers are for the engines).
            "students": {"learn": False},
        },
        "owners": {"chat": ["boss"], "owner": ["boss"]},
        "control": "owner:dm",
        "providers": {},
        "profiles": {"default": [{"provider": "fake", "model": "fake-model"}]},
        "personas": {"p": {"display_name": "Pat", "prompt": "You are Pat.", "profile": "default"}},
        "assignments": {"*": {"engine": "probe", "personas": ["p"]}},
    }
    raw = merge(raw, overrides or {})
    # Tests name exactly the modules they load (`extensions`: a list of folders), with no folder scanning.
    raw["modules"] = {"dirs": [], "paths": raw.pop("extensions"), **(raw.get("modules") or {})}
    config = CoreConfig(raw, tmp)
    config.file = tmp / "core.yml"  # $setup must never edit the repository's own core.yml
    config.env_file = tmp / ".env"
    return config


async def start_core(tmp: Path, overrides: dict | None = None, adapter: FakeAdapter | None = None,
                     clock=None) -> Core:
    core = Core(make_config(tmp, overrides), provider_adapters={"fake": adapter or FakeAdapter()},
                tick_seconds=3600, clock=clock)
    await core.start()
    return core


async def settle(core: Core) -> None:
    for _ in range(50):
        await asyncio.sleep(0)
        tasks = [*core.manager._tasks.values(), *core.gateway._tasks]
        if not tasks:
            await asyncio.sleep(0)
            if not core.manager._tasks and not core.gateway._tasks:
                return
            continue
        await asyncio.gather(*tasks, return_exceptions=True)


def iface(core: Core, name: str = "chat"):
    return core.gateway.interfaces[name]


def extension_module(core: Core, name: str):
    return next(item.module for item in core.extensions if item.extension.name == name)

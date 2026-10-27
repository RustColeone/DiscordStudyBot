"""The ways the club can work on a change, one file each. Drop a file in this folder to add one:

    from . import workflow

    @workflow("pair", "Pair", "Two classmates draft in turn; the second builds on the first")
    async def pair(round):
        first = await round.solve(f"{round.r}.first", "drafter", "draft", round.base_brief)
        ...
        return round.outcome(solved, await round.diff("pair.diff"), await round.validate("pair.test"))

`round` is the round's toolkit (kernel/flow.py `Round`): `solve` (a classmate works with the tools), `staffed`
(the first classmate of a role who answers), `review_change` (a judge or critic reads the diff), `validate` (the
tests), `diff` (the change set), `outcome`, `first`, `say_route`, and `ctx` (the journal: model calls, tool calls
and lines said happen once, so a restart replays instead of repeating). Whatever a workflow returns is verified the
same way afterwards (the requirements, the scope, the tester's probes; see kernel/verify.py), so it need not check
everything itself.

Pick one per task (the page, `club --workflow pair`), or route requests to it in config.yml:
`routing: {small: pair}` (keys: question, tiny, small, medium, large, risky).
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass(frozen=True)
class Workflow:
    name: str
    title: str
    text: str
    run: Callable[..., Awaitable]


REGISTRY: dict[str, Workflow] = {}


def workflow(name: str, title: str, text: str):
    """Register `async def run(round) -> Outcome` under `name` (one name, one workflow: a later file may not take
    an existing name)."""
    def register(function):
        if name in REGISTRY and REGISTRY[name].run is not function:
            raise ValueError(f"A workflow named {name!r} already exists")
        REGISTRY[name] = Workflow(name, title, text, function)
        return function
    return register


def load() -> dict[str, Workflow]:
    """Every workflow in this folder (the built-in ones first)."""
    for name in ("single", "cascade", "critique", "plan"):
        importlib.import_module(f"{__name__}.{name}")
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
    return REGISTRY


def catalog() -> list[dict]:
    """What the frontends offer: auto (the club picks), then every workflow."""
    load()
    return [{"name": "auto", "title": "Auto", "text": "The club picks the simplest workflow likely to be enough"},
            *({"name": item.name, "title": item.title, "text": item.text} for item in REGISTRY.values())]

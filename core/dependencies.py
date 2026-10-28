"""Declared dependencies (contracts 1.10): the order modules load in, and whether a Python package a module requires
is installed.

A module declares the modules it `requires` and `uses` (contracts.Extension). Each loads after those of them that
are there, so its setup can be told which it has; modules that depend on each other in a circle cannot load.
"""

from __future__ import annotations

import importlib.metadata
import re
from typing import Mapping

from contracts import Extension


def load_order(extensions: Mapping[str, Extension]) -> tuple[list[str], dict[str, str]]:
    """The module names in load order (each after the modules it requires or uses that are present, otherwise in
    the given order), and the modules caught in a dependency cycle, each with why."""
    present = set(extensions)
    needs = {name: [item for item in (*extension.requires, *extension.uses) if item in present]
             for name, extension in extensions.items()}
    order, state, cyclic = [], {}, {}

    def visit(name: str, path: list[str]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "visiting":
            circle = [*path[path.index(name):], name]
            for item in circle[:-1]:
                cyclic.setdefault(item, "it is in a dependency cycle: " + " → ".join(circle))
            return
        state[name] = "visiting"
        for item in needs[name]:
            visit(item, [*path, name])
        state[name] = "done"
        order.append(name)

    for name in extensions:
        visit(name, [])
    return [name for name in order if name not in cyclic], cyclic


def missing_package(requirement: str) -> str | None:
    """None when `package:<name>` (or `package:<name>>=<version>`) is installed, at that version or later;
    otherwise what is missing, in words."""
    name, _, floor = requirement.removeprefix("package:").partition(">=")
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return f"the Python package {name}" + (f" ({floor} or later)" if floor else "") + ", which is not installed"
    if floor and _version(installed) < _version(floor):
        return f"the Python package {name} {floor} or later, but {installed} is installed"
    return None


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text.split("+")[0])[:4])

"""Facts: small public values modules publish about personas, for interfaces to show (contracts 1.9).

Kept in the tank's store `core:facts`, one entry per persona (`["gpt"]`) and per persona and person
(`["gpt", "webui", "creator"]`), each `{"<module>.<key>": value}`. Modules write through their own handle, so
none can change another's facts; interfaces read through their port, only about their own people.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from core.storage.tank import Tank

FACTS = "core:facts"
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
LIMIT = 4096  # characters of a value once encoded


def slot(persona: str, person: tuple[str, str] | None = None) -> str:
    return json.dumps([persona] if person is None else [persona, *person], ensure_ascii=False)


def check_person(person) -> tuple[str, str] | None:
    if person is None:
        return None
    if (not isinstance(person, (tuple, list)) or len(person) != 2
            or not all(isinstance(part, str) and part for part in person)):
        raise ValueError("person is (interface, sender id), as Exchange.person gives it")
    return (person[0], person[1])


class FactsDesk:
    def __init__(self, tank: "Tank"):
        self.store = tank.store(FACTS)

    def read(self, persona: str, person: tuple[str, str] | None = None) -> dict[str, Any]:
        """The persona's facts, with the person's over them."""
        found = dict(self.store.get(slot(persona)) or {})
        if person is not None:
            found.update(self.store.get(slot(persona, check_person(person))) or {})
        return found

    def write(self, module: str, persona: str, key: str, value: Any, person=None) -> None:
        if not isinstance(persona, str) or not persona:
            raise ValueError("A fact is about a persona: give her name")
        if not isinstance(key, str) or not KEY.match(key):
            raise ValueError(f"A fact's key is lowercase letters, digits or _, got {key!r}")
        where = slot(persona, check_person(person))
        facts = dict(self.store.get(where) or {})
        name = f"{module}.{key}"
        if value is None:
            facts.pop(name, None)
        else:
            try:
                encoded = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                raise ValueError(f"fact {name!r}: the value is not JSON") from None
            if len(encoded) > LIMIT:
                raise ValueError(f"fact {name!r}: at most {LIMIT} characters once encoded")
            facts[name] = json.loads(encoded)
        if facts:
            self.store.set(where, facts)
        else:
            self.store.delete(where)

    def rename(self, old: str, new: str) -> int:
        """Persona.formerly: her facts under `old` move to `new` (what `new` has wins). Returns how many entries."""
        moved = 0
        for where in self.store.keys():
            try:
                parts = json.loads(where)
            except ValueError:
                continue
            if not isinstance(parts, list) or not parts or parts[0] != old:
                continue
            target = json.dumps([new, *parts[1:]], ensure_ascii=False)
            self.store.set(target, {**dict(self.store.get(where) or {}), **dict(self.store.get(target) or {})})
            self.store.delete(where)
            moved += 1
        return moved

    def rename_module(self, old: str, new: str) -> int:
        """Extension.formerly: the facts module `old` published are module `new`'s (what `new` has wins). Returns how
        many entries changed."""
        moved = 0
        for where in self.store.keys():
            facts = dict(self.store.get(where) or {})
            old_keys = [name for name in facts if name.startswith(old + ".")]
            if not old_keys:
                continue
            for name in old_keys:
                facts.setdefault(new + name[len(old):], facts[name])
                facts.pop(name)
            self.store.set(where, facts)
            moved += 1
        return moved

    def handle(self, module: str) -> "FactsHandle":
        return FactsHandle(self, module)


class FactsHandle:
    """Implements contracts.Facts for one module."""

    def __init__(self, desk: FactsDesk, module: str):
        self._desk, self._module = desk, module

    def set(self, persona: str, key: str, value: Any, *, person: tuple[str, str] | None = None) -> None:
        self._desk.write(self._module, persona, key, value, person)

    def get(self, persona: str, key: str, *, person: tuple[str, str] | None = None) -> Any:
        where = slot(persona, check_person(person))
        return (self._desk.store.get(where) or {}).get(f"{self._module}.{key}")

    def delete(self, persona: str, key: str, *, person: tuple[str, str] | None = None) -> None:
        self._desk.write(self._module, persona, key, None, person)

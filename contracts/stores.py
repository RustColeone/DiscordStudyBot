"""Scoped persistent key-value storage handed to blocks by the core."""

from __future__ import annotations

from typing import Any, Iterable, Protocol


class Store(Protocol):
    """A private JSON key-value namespace. Values must be JSON-serializable.

    Each extension, persona or interface gets its own namespace; one cannot read another's keys.
    """

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    def delete(self, key: str) -> None: ...

    def keys(self, prefix: str = "") -> Iterable[str]: ...


class Facts(Protocol):
    """Small public values a module publishes about a persona (added in 1.9): her mood or level, or, with
    `person`, where she stands with one person (their bond, the name they call her). Interfaces read them with
    `InterfacePort.facts` to show them on a page; nothing else does, so a fact is never a module's private state.

    A module writes only its own keys (lowercase letters, digits and `_`); readers see them as `<module>.<key>`.
    `person` is `(interface, sender id)`, the person as one interface knows them (`Exchange.person`). A value is
    JSON, at most 4 KB once encoded; setting None deletes it. Facts last across restarts."""

    def set(self, persona: str, key: str, value: Any, *, person: tuple[str, str] | None = None) -> None: ...

    def get(self, persona: str, key: str, *, person: tuple[str, str] | None = None) -> Any: ...

    def delete(self, persona: str, key: str, *, person: tuple[str, str] | None = None) -> None: ...

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

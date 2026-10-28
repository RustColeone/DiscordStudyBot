"""Scoped persistent key-value storage handed to blocks by the core."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol


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


class Prices(Protocol):
    """Model prices, as the core keeps them (added in 1.12): a list fetched once a day (Artificial Analysis's, with a
    key) under the owner's own figures (core.yml `prices:`). Every ModelResult already carries its price and cost;
    this is for pages and engines that show prices or estimate before a call."""

    def price(self, provider: str, model: str) -> Mapping[str, Any] | None:
        """Dollars per million input and output tokens (and cache hits, when listed), with where the figure came
        from; None when unknown (never a guess)."""

    def cost(self, price: Mapping[str, Any] | None, input_tokens: int, output_tokens: int,
             cached_input_tokens: int = 0) -> float | None: ...

    def status(self, models: Iterable[Mapping[str, Any]] = ()) -> Mapping[str, Any]:
        """Where prices come from (with the attribution their source asks for), how old the list is, and the price of
        each of `models` (`{provider, model, ...}` rows, given back with `price` and, when unknown, `candidates`)."""

    async def refresh_if_stale(self) -> str: ...

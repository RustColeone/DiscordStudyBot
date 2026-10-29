"""Prompt parts: the pieces of a persona's prompt that engines place for it (see contracts.PromptPart).

A persona's own parts come from its module or from core.yml (`personas: <name>: parts:`); modules may add parts
for every persona or for those they name. `Session.prompt` renders them for one persona at one place, and
`Session.aprompt` awaits the async ones too."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from contracts import PART_PLACES, PartContext, Persona, PromptPart

PLACE_LIMIT = 40_000  # characters per persona and place: the part that crosses it is cut, those after are left out
FILE_LIMIT = 1_000_000  # characters read from a part's file (comments included)
CUT = " [...]"
COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)  # an unclosed comment runs to the end, as in HTML
PART_KEYS = frozenset({"name", "text", "file", "place", "order"})
PART_WAIT_S = 8.0  # seconds an async part may take before it is left out


class FileText:
    """A part's text kept in a file, read each time it is rendered: an edit applies from the next turn, without a
    restart. HTML comments (`<!-- ... -->`) are notes for whoever edits the file, and are left out."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def __call__(self, context: PartContext) -> str:
        if not self.path.is_file():  # missing, or not a regular file (a FIFO would block the run)
            raise FileNotFoundError(f"no file {self.path}")
        with self.path.open(encoding="utf-8") as file:
            return COMMENT.sub("", file.read(FILE_LIMIT))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, FileText) and other.path == self.path

    def __hash__(self) -> int:
        return hash(self.path)

    def __repr__(self) -> str:
        return f"FileText({str(self.path)!r})"


def parts_from_config(persona: str, value: Any, resolve: Callable[[str], Path]) -> tuple[PromptPart, ...]:
    """A core.yml persona's `parts:`: a list of {name, text | file, place, order}. A file is a path relative to
    core.yml's folder. Names default to the file's stem, or `part <n>`."""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"persona {persona!r}: `parts:` is a list")
    parts: list[PromptPart] = []
    for index, item in enumerate(value, 1):
        where = f"persona {persona!r}, part {index}"
        if not isinstance(item, Mapping) or set(item) - PART_KEYS:
            raise ValueError(f"{where}: a part takes {', '.join(sorted(PART_KEYS))}")
        if ("text" in item) == ("file" in item):
            raise ValueError(f"{where}: give either `text` or `file`")
        if "file" in item:
            if not isinstance(item["file"], str) or not item["file"].strip():
                raise ValueError(f"{where}: `file` is a path")
            path = resolve(item["file"].strip())
            text: Any = FileText(path)
            default = path.stem
        else:
            if not isinstance(item["text"], str):
                raise ValueError(f"{where}: `text` is text (quote it)")
            text, default = item["text"], f"part {index}"
        name = item.get("name", default)
        order = item.get("order", 100)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: `name` is text")
        if isinstance(order, bool) or not isinstance(order, int):
            raise ValueError(f"{where}: `order` is a whole number")
        if any(part.name == name.strip() for part in parts):
            raise ValueError(f"{where}: another part is already named {name.strip()!r}")
        parts.append(PromptPart(name.strip(), text, str(item.get("place", "stable")), order))
    return tuple(parts)


def with_parts(persona: Persona, added: Iterable[PromptPart]) -> Persona:
    """The persona with `added` parts: one named like a part it has replaces it, in its place."""
    merged = {part.name: part for part in persona.parts}
    merged.update({part.name: part for part in added})
    return dataclasses.replace(persona, parts=tuple(merged.values()))


def parts_for(persona: Persona, shared: Iterable[PromptPart]) -> list[PromptPart]:
    """The persona's own parts, then the modules' parts for it (those naming it, or naming no persona). Its own
    part shadows a module's part of the same name, so core.yml can replace one, or switch it off with `text: ""`."""
    own = list(persona.parts)
    names = {part.name for part in own}
    return own + [part for part in shared
                  if (not part.personas or persona.name in part.personas) and part.name not in names]


def describe(error: BaseException) -> str:
    try:
        return f"{type(error).__name__}: {error}"
    except Exception:  # an exception whose message itself fails
        return type(error).__name__


def _ordered(parts: Iterable[PromptPart], place: str) -> list[PromptPart]:
    if place not in PART_PLACES:
        raise ValueError(f"place is one of {', '.join(PART_PLACES)}")
    return sorted((part for part in parts if part.place == place), key=lambda part: part.order)


def _text(text) -> str:
    if not isinstance(text, str):
        raise TypeError(f"gave {type(text).__name__}, not text")
    return text


def render(parts: Iterable[PromptPart], context: PartContext, place: str,
           report: Callable[[PromptPart, str, str], None]) -> str:
    """The parts at `place`, in order (ties keep the order given), each stripped and joined by a blank line, at most
    PLACE_LIMIT characters. A part whose text fails or is not text is left out, and `report(part, kind, detail)`
    is called with kind `failed`; one cut to fit is reported `cut`, those after it `left_out`. An async part is
    left out (`arender` awaits it)."""
    found = []
    for part in _ordered(parts, place):
        try:
            text = part.text if isinstance(part.text, str) else part.text(context)
            if inspect.isawaitable(text):
                if inspect.iscoroutine(text):
                    text.close()
                raise TypeError("its text function is async: it is rendered with Session.aprompt")
            found.append((part, _text(text)))
        except Exception as error:  # a module's or a file's problem must not fail the run
            report(part, "failed", "left out: " + describe(error))
    return _join(found, place, report)


async def arender(parts: Iterable[PromptPart], context: PartContext, place: str,
                  report: Callable[[PromptPart, str, str], None]) -> str:
    """`render`, with each async part awaited in turn for at most PART_WAIT_S seconds (one that takes longer is
    left out and reported `failed`)."""
    found = []
    for part in _ordered(parts, place):
        try:
            text = part.text if isinstance(part.text, str) else part.text(context)
            if inspect.isawaitable(text):
                text = await asyncio.wait_for(text, PART_WAIT_S)
            found.append((part, _text(text)))
        except TimeoutError:
            report(part, "failed", f"left out: it took more than {PART_WAIT_S:g} s")
        except Exception as error:  # a module's or a file's problem must not fail the run
            report(part, "failed", "left out: " + describe(error))
    return _join(found, place, report)


def _join(found: list[tuple[PromptPart, str]], place: str, report: Callable[[PromptPart, str, str], None]) -> str:
    texts: list[str] = []
    size, full = 0, False
    for part, text in found:
        text = text.strip()
        if not text:
            continue
        if not full and len(text) > PLACE_LIMIT - size:
            full = True
            kept = text[:max(PLACE_LIMIT - size - len(CUT), 0)].rstrip()
            if kept:
                report(part, "cut", f"cut to fit: the {place} parts keep to {PLACE_LIMIT} characters")
                texts.append(kept + CUT)
                continue
        if full:
            report(part, "left_out", f"left out: the {place} parts reached {PLACE_LIMIT} characters")
            continue
        texts.append(text)
        size += len(text) + 2
    return "\n\n".join(texts)

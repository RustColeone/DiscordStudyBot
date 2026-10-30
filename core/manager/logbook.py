"""The log: what happened in a conversation, one line each, kept by the core for every module.

    a module (capability, engine, watcher) ──► logbook.write(kind, text) ──► the tank's `log`, filed by conversation
    the core, after each write or critical action that ran ───────────────► a line of kind `action`
    engines read `session.logbook` (MomoChat shows the last few lines in each turn); people read `$log`

Lines are never edited: a correction is a new line. It is not the audit log, the core's own record for the owner.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Mapping

from contracts import LogEntry

if TYPE_CHECKING:
    from core.manager.manager import Manager

MAX_TEXT = 500
MAX_DATA = 2000
LINES_KEPT = 2000  # in one conversation; the oldest go
KIND = re.compile(r"[a-z][a-z0-9_-]{0,31}")
IGNORED_TERMS = {"the", "that", "this", "you", "your", "what", "about", "with", "have",
                 "你们", "你們", "主人", "记得", "記得", "觉得", "覺得", "什么", "什麼"}


def search_terms(text: str) -> list[str]:
    """Latin words of 3+ letters and overlapping two-character pieces of CJK runs."""
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z]{3,}|[一-鿿]{2,}", text):
        pieces = [token.lower()] if token.isascii() else [token[i:i + 2] for i in range(min(len(token) - 1, 60))]
        for piece in pieces:
            if piece not in IGNORED_TERMS and piece not in terms:
                terms.append(piece)
    return terms[:24]


class LogbookHandle:
    """Implements contracts.Logbook for one module in one conversation."""

    def __init__(self, manager: "Manager", address: str, module: str, subject: str = ""):
        self._manager, self._address, self._module, self._subject = manager, address, module, subject

    def write(self, kind: str, text: str, *, person: bool = False, persona: str = "",
              data: Mapping[str, Any] | None = None) -> LogEntry:
        if not isinstance(kind, str) or not KIND.fullmatch(kind):
            raise ValueError("A log line's kind is one short lowercase word")
        line = " ".join(str(text).split())
        if not line:
            raise ValueError("A log line needs text")
        scrubbed = self._manager.secrets.scrub_value(dict(data or {}))
        if len(str(scrubbed)) > MAX_DATA:
            raise ValueError(f"A log line's data is at most {MAX_DATA} characters")
        return self._manager.tank.add_log(self._address, self._module, kind, self._manager.secrets.scrub(line)[:MAX_TEXT],
                                          subject=self._subject if person else "", persona=persona, data=scrubbed,
                                          keep=LINES_KEPT)

    def recent(self, limit: int = 8, kind: str = "") -> list[LogEntry]:
        return self._manager.tank.log_lines(self._address, max(1, min(int(limit), 50)), kind=kind)[::-1]

    def find(self, text: str, limit: int = 8) -> list[LogEntry]:
        terms = search_terms(text)
        return self._manager.tank.log_lines(self._address, max(1, min(int(limit), 50)), terms=terms) if terms else []

"""Append-only audit and timing log (JSON lines). No message text or arguments are written here."""

from __future__ import annotations

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class Audit:
    def __init__(self, path: str | Path | None = None, echo: bool = False):
        self._logger = logging.getLogger(f"core.audit.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.INFO)
        self.echo = echo
        self.events: list[dict[str, Any]] = []
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            self._logger.addHandler(handler)

    def record(self, event: str, **fields: Any) -> None:
        entry = {"at": round(time.time(), 3), "event": event, **fields}
        self.events.append(entry)
        del self.events[:-500]
        line = json.dumps(entry, ensure_ascii=False, default=str)
        self._logger.info(line)
        if self.echo:
            print("[core] " + line, flush=True)

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)

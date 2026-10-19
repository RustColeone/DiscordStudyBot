"""Small SQLite helpers shared by the tank and the bottle. Each database is a separate file."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable


class Database:
    def __init__(self, path: str | Path, schema: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # SQLite's own case handling is ASCII-only; casefold() also matches "café" with "CAFÉ", "Москва"
        # with "москва".
        self._conn.create_function("casefold", 1, lambda value: value.casefold() if isinstance(value, str) else value,
                                   deterministic=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(schema)

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(parameters))

    def query(self, sql: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(parameters)).fetchall()

    def one(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(parameters)).fetchone()

    def transaction(self):
        return _Transaction(self)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _Transaction:
    def __init__(self, database: Database):
        self.database = database

    def __enter__(self):
        self.database._lock.acquire()
        self.database._conn.execute("BEGIN IMMEDIATE")
        return self.database

    def __exit__(self, kind, value, traceback):
        try:
            self.database._conn.execute("ROLLBACK" if kind else "COMMIT")
        finally:
            self.database._lock.release()
        return False


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    return default if text is None else json.loads(text)


class ScopedStore:
    """A JSON key-value namespace inside one table. Implements contracts.Store."""

    def __init__(self, database: Database, table: str, scope: str):
        if not table.isidentifier():
            raise ValueError("Invalid table name")
        self._database, self._table, self.scope = database, table, scope

    def get(self, key: str, default: Any = None) -> Any:
        row = self._database.one(f"SELECT value FROM {self._table} WHERE scope=? AND key=?", (self.scope, str(key)))
        return default if row is None else loads(row["value"])

    def set(self, key: str, value: Any) -> None:
        self._database.execute(
            f"INSERT INTO {self._table}(scope,key,value) VALUES(?,?,?) "
            "ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value",
            (self.scope, str(key), dumps(value)))

    def delete(self, key: str) -> None:
        self._database.execute(f"DELETE FROM {self._table} WHERE scope=? AND key=?", (self.scope, str(key)))

    def keys(self, prefix: str = "") -> list[str]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._database.query(
            f"SELECT key FROM {self._table} WHERE scope=? AND key LIKE ? ESCAPE '\\' ORDER BY key",
            (self.scope, escaped + "%"))
        return [row["key"] for row in rows]

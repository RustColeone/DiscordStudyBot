"""The bottle: what the gateway needs to talk to platforms. Separate from the tank (gateway.db)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.storage.sqlite import Database, ScopedStore, dumps, loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rules (pattern TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routes (pattern TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routines (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS receipts (key TEXT PRIMARY KEY, origin TEXT NOT NULL, at REAL NOT NULL);
"""

SEEN_RETENTION_S = 30 * 86400


class Bottle:
    def __init__(self, path: str | Path):
        self.db = Database(path, SCHEMA)
        self.db.execute("DELETE FROM seen WHERE at<?", (time.time() - SEEN_RETENTION_S,))
        self.db.execute("DELETE FROM receipts WHERE at<?", (time.time() - SEEN_RETENTION_S,))

    def close(self) -> None:
        self.db.close()

    def interface_store(self, interface: str) -> ScopedStore:
        return ScopedStore(self.db, "kv", "interface:" + interface)

    def mark_seen(self, key: str) -> bool:
        """Record a request key. False if it was already seen."""
        cursor = self.db.execute("INSERT OR IGNORE INTO seen(key,at) VALUES(?,?)", (key, time.time()))
        return cursor.rowcount == 1

    def forget_seen(self, key: str) -> None:
        self.db.execute("DELETE FROM seen WHERE key=?", (key,))

    def add_receipt(self, key: str, origin: str) -> None:
        """Remember which conversation a delivered event came from, so an owner's reply can be relayed."""
        self.db.execute("INSERT OR REPLACE INTO receipts(key,origin,at) VALUES(?,?,?)", (key, origin, time.time()))

    def receipt(self, key: str) -> str | None:
        row = self.db.one("SELECT origin FROM receipts WHERE key=?", (key,))
        return None if row is None else row["origin"]

    def _table(self, table: str) -> dict[str, Any]:
        return {row["pattern" if table != "routines" else "name"]: loads(row["value"])
                for row in self.db.query(f"SELECT * FROM {table}")}

    def rules(self) -> dict[str, dict]:
        return self._table("rules")

    def set_rule(self, pattern: str, value: dict | None) -> None:
        self._put("rules", "pattern", pattern, value)

    def routes(self) -> dict[str, dict]:
        return self._table("routes")

    def set_route(self, pattern: str, value: dict | None) -> None:
        self._put("routes", "pattern", pattern, value)

    def routines(self) -> dict[str, dict]:
        return self._table("routines")

    def set_routine(self, name: str, value: dict | None) -> None:
        self._put("routines", "name", name, value)

    def _put(self, table: str, column: str, key: str, value: dict | None) -> None:
        if value is None:
            self.db.execute(f"DELETE FROM {table} WHERE {column}=?", (key,))
        else:
            self.db.execute(f"INSERT INTO {table}({column},value) VALUES(?,?) "
                            f"ON CONFLICT({column}) DO UPDATE SET value=excluded.value", (key, dumps(value)))

"""The tank: everything the bot knows. Only the Manager uses it."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import Event, HistoryItem
from core.storage.sqlite import Database, ScopedStore, dumps, loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, direction TEXT NOT NULL,
    message_id TEXT, sender_id TEXT, sender_name TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
    run_id TEXT, reply_to TEXT, quoted_text TEXT, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS messages_address ON messages(address, seq);
CREATE TABLE IF NOT EXISTS kv (scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY(scope, key));
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, address TEXT NOT NULL, lane TEXT NOT NULL, engine TEXT NOT NULL, personas TEXT NOT NULL,
    assignment TEXT NOT NULL, status TEXT NOT NULL, origin TEXT NOT NULL, trigger TEXT NOT NULL,
    state TEXT NOT NULL, inbox_cursor INTEGER NOT NULL DEFAULT 0, usage TEXT NOT NULL,
    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, lane);
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL REFERENCES runs(id), idx INTEGER NOT NULL, kind TEXT NOT NULL,
    at REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id, idx));
CREATE TABLE IF NOT EXISTS actions (
    run_id TEXT NOT NULL REFERENCES runs(id), call_key TEXT NOT NULL, name TEXT NOT NULL,
    arguments TEXT NOT NULL, risk TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
    started_at REAL NOT NULL, finished_at REAL, PRIMARY KEY(run_id, call_key));
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id), call_key TEXT NOT NULL,
    name TEXT NOT NULL, arguments TEXT NOT NULL, address TEXT NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL, decided_at REAL, decided_by TEXT);
"""

ACTIVE = ("running", "waiting_input")
OPEN = ("running", "waiting_input", "waiting_approval")


@dataclass
class RunRecord:
    id: str
    address: str
    lane: str
    engine: str
    personas: list[str]
    assignment: dict[str, Any]
    status: str
    origin: str
    trigger: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    inbox_cursor: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class Tank:
    def __init__(self, path: str | Path):
        self.db = Database(path, SCHEMA)

    def close(self) -> None:
        self.db.close()

    def store(self, scope: str) -> ScopedStore:
        return ScopedStore(self.db, "kv", scope)

    def record_message(self, address: str, direction: str, text: str, *, kind: str = "text",
                       sender_id: str = "", sender_name: str = "", message_id: str | None = None,
                       run_id: str | None = None, reply_to: str | None = None, quoted_text: str = "",
                       at: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO messages(address,direction,message_id,sender_id,sender_name,text,kind,run_id,reply_to,"
            "quoted_text,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (address, direction, message_id, sender_id, sender_name, text, kind, run_id, reply_to, quoted_text or "",
             at or time.time()))

    def has_message(self, address: str, direction: str, message_id: str) -> bool:
        return self.db.one("SELECT 1 FROM messages WHERE address=? AND direction=? AND message_id=?",
                           (address, direction, message_id)) is not None

    def recent_messages(self, address: str, limit: int = 30) -> list[HistoryItem]:
        rows = self.db.query("SELECT * FROM messages WHERE address=? ORDER BY seq DESC LIMIT ?",
                             (address, max(1, min(int(limit), 200))))
        return [HistoryItem(direction=row["direction"], text=row["text"], at=row["at"],
                            sender_name=row["sender_name"] or "", sender_id=row["sender_id"] or "",
                            kind=row["kind"], message_id=row["message_id"], reply_to=row["reply_to"],
                            quoted_text=row["quoted_text"] or "")
                for row in reversed(rows)]

    def create_run(self, run: RunRecord) -> RunRecord:
        now = time.time()
        run.created_at = run.updated_at = now
        self.db.execute(
            "INSERT INTO runs(id,address,lane,engine,personas,assignment,status,origin,trigger,state,inbox_cursor,"
            "usage,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run.id, run.address, run.lane, run.engine, dumps(run.personas), dumps(run.assignment), run.status,
             run.origin, dumps(run.trigger), dumps(run.state), run.inbox_cursor, dumps(run.usage),
             run.error, now, now))
        return run

    def _run(self, row) -> RunRecord:
        return RunRecord(id=row["id"], address=row["address"], lane=row["lane"], engine=row["engine"],
                         personas=loads(row["personas"]), assignment=loads(row["assignment"]),
                         status=row["status"], origin=row["origin"], trigger=loads(row["trigger"]),
                         state=loads(row["state"]), inbox_cursor=row["inbox_cursor"], usage=loads(row["usage"]),
                         error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"])

    def run(self, run_id: str) -> RunRecord | None:
        row = self.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        return None if row is None else self._run(row)

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "state", "inbox_cursor", "usage", "error"}
        if not fields or set(fields) - allowed:
            raise ValueError("Unsupported run fields")
        encoded = {key: dumps(value) if key in {"state", "usage"} else value for key, value in fields.items()}
        assignments = ",".join(f"{key}=?" for key in encoded)
        self.db.execute(f"UPDATE runs SET {assignments}, updated_at=? WHERE id=?",
                        (*encoded.values(), time.time(), run_id))

    def runs(self, statuses: tuple[str, ...] = OPEN, address: str | None = None, limit: int = 100) -> list[RunRecord]:
        marks = ",".join("?" for _ in statuses)
        sql = f"SELECT * FROM runs WHERE status IN ({marks})"
        parameters: list[Any] = list(statuses)
        if address is not None:
            sql += " AND address=?"
            parameters.append(address)
        rows = self.db.query(sql + " ORDER BY created_at DESC LIMIT ?", (*parameters, limit))
        return [self._run(row) for row in rows]

    def lane_runs(self, lane: str) -> list[RunRecord]:
        """Open runs of a lane that can take a message (running or waiting for input), newest first."""
        rows = self.db.query("SELECT * FROM runs WHERE lane=? AND status IN ('running','waiting_input') "
                             "ORDER BY created_at DESC", (lane,))
        return [self._run(row) for row in rows]

    def active_run(self, lane: str) -> RunRecord | None:
        rows = self.db.query("SELECT * FROM runs WHERE lane=? AND status IN ('running','waiting_input') "
                             "ORDER BY created_at DESC LIMIT 1", (lane,))
        return self._run(rows[0]) if rows else None

    def append_event(self, run_id: str, kind: str, data: dict[str, Any] | None = None) -> Event:
        with self.db.transaction():
            row = self.db.one("SELECT COALESCE(MAX(idx), -1) + 1 AS next FROM run_events WHERE run_id=?", (run_id,))
            index, now = row["next"], time.time()
            self.db.execute("INSERT INTO run_events(run_id,idx,kind,at,data) VALUES(?,?,?,?,?)",
                            (run_id, index, kind, now, dumps(data or {})))
        return Event(index=index, kind=kind, at=now, data=data or {})

    def events(self, run_id: str, kinds: tuple[str, ...] | None = None, after: int = -1) -> list[Event]:
        rows = self.db.query("SELECT * FROM run_events WHERE run_id=? AND idx>? ORDER BY idx", (run_id, after))
        return [Event(index=row["idx"], kind=row["kind"], at=row["at"], data=loads(row["data"]))
                for row in rows if kinds is None or row["kind"] in kinds]

    def action(self, run_id: str, call_key: str):
        row = self.db.one("SELECT * FROM actions WHERE run_id=? AND call_key=?", (run_id, call_key))
        if row is None:
            return None
        return {"name": row["name"], "arguments": loads(row["arguments"]), "risk": row["risk"],
                "status": row["status"], "result": loads(row["result"])}

    def start_action(self, run_id: str, call_key: str, name: str, arguments: dict, risk: str) -> None:
        self.db.execute(
            "INSERT INTO actions(run_id,call_key,name,arguments,risk,status,started_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(run_id,call_key) DO UPDATE SET status='started', started_at=excluded.started_at, "
            "result=NULL, finished_at=NULL",
            (run_id, call_key, name, dumps(arguments), risk, "started", time.time()))

    def finish_action(self, run_id: str, call_key: str, result: dict) -> None:
        self.db.execute("UPDATE actions SET status='done', result=?, finished_at=? WHERE run_id=? AND call_key=?",
                        (dumps(result), time.time(), run_id, call_key))

    def count_actions(self, run_id: str) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM actions WHERE run_id=?", (run_id,))["n"]

    def create_approval(self, approval_id: str, run_id: str, call_key: str, name: str,
                        arguments: dict, address: str) -> None:
        self.db.execute(
            "INSERT INTO approvals(id,run_id,call_key,name,arguments,address,status,created_at) "
            "VALUES(?,?,?,?,?,?,'pending',?)",
            (approval_id, run_id, call_key, name, dumps(arguments), address, time.time()))

    def _approval(self, row) -> dict:
        return {"id": row["id"], "run_id": row["run_id"], "call_key": row["call_key"], "name": row["name"],
                "arguments": loads(row["arguments"]), "address": row["address"], "status": row["status"],
                "created_at": row["created_at"], "decided_by": row["decided_by"]}

    def approval(self, approval_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return None if row is None else self._approval(row)

    def approvals(self, status: str = "pending", run_id: str | None = None) -> list[dict]:
        sql, parameters = "SELECT * FROM approvals WHERE status=?", [status]
        if run_id is not None:
            sql, parameters = sql + " AND run_id=?", [*parameters, run_id]
        return [self._approval(row) for row in self.db.query(sql + " ORDER BY created_at", parameters)]

    def decide_approval(self, approval_id: str, status: str, decided_by: str) -> bool:
        """Atomically move a pending approval to approved/denied/expired. False if it was not pending."""
        cursor = self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            (status, time.time(), decided_by, approval_id))
        return cursor.rowcount == 1

"""The tank: everything the bot knows. Only the Manager uses it."""

from __future__ import annotations

import dataclasses
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from contracts import Event, FileRef, HistoryItem, LogEntry
from core.storage.sqlite import Database, ScopedStore, dumps, fold, loads

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, direction TEXT NOT NULL,
    message_id TEXT, sender_id TEXT, sender_name TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
    run_id TEXT, reply_to TEXT, quoted_text TEXT, at REAL NOT NULL, files TEXT);
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
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, module TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    address TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '', persona TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS log_address ON log(address, id);
CREATE INDEX IF NOT EXISTS log_person ON log(address, subject, persona);
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
        if "files" not in {row[1] for row in self.db.query("PRAGMA table_info(messages)")}:
            self.db.execute("ALTER TABLE messages ADD COLUMN files TEXT")
        if self.db.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='diary'"):  # the log's name before
            with self.db.transaction() as db:
                db.execute("INSERT INTO log(at,module,kind,text,address,subject,persona,data) SELECT updated_at,'core',"
                           "'note',note,address,subject,persona,json_object('key',key,'kind',kind,'importance',importance)"
                           " FROM diary")
                db.execute("DROP TABLE diary")

    def close(self) -> None:
        self.db.close()

    def store(self, scope: str) -> ScopedStore:
        return ScopedStore(self.db, "kv", scope)

    def rename_persona(self, old: str, new: str, display_name: str) -> dict[str, int]:
        """Move what is filed under persona `old` to `new` (Persona.formerly): her memory and every module's stores
        scoped by her, her lines in the log, the runs she is in, and her name on what she said. What `new` already has
        wins. Returns how many of each were moved (all zero once done)."""
        moved = {"stores": 0, "notes": 0, "runs": 0, "messages": 0}
        with self.db.transaction() as db:
            for row in db.query("SELECT DISTINCT scope FROM kv WHERE scope=? OR scope LIKE 'extension:% {%'",
                                ("persona:" + old,)):
                scope = row["scope"]
                if scope == "persona:" + old:
                    target = "persona:" + new
                else:
                    head, _, parts = scope.partition(" ")
                    try:
                        scoped = json.loads(parts)
                    except ValueError:
                        continue
                    if not isinstance(scoped, dict) or scoped.get("persona") != old:
                        continue
                    target = head + " " + json.dumps({**scoped, "persona": new}, ensure_ascii=False, sort_keys=True)
                db.execute("INSERT OR IGNORE INTO kv(scope, key, value) SELECT ?, key, value FROM kv WHERE scope=?",
                           (target, scope))
                moved["stores"] += db.execute("DELETE FROM kv WHERE scope=?", (scope,)).rowcount
            moved["notes"] = db.execute("UPDATE log SET persona=? WHERE persona=?", (new, old)).rowcount
            for row in db.query("SELECT id, personas, assignment FROM runs WHERE personas LIKE ?",
                                ("%" + json.dumps(old) + "%",)):
                personas = loads(row["personas"], [])
                if old not in personas:
                    continue
                assignment = loads(row["assignment"], {})
                if isinstance(assignment.get("personas"), list):
                    assignment["personas"] = [new if name == old else name for name in assignment["personas"]]
                db.execute("UPDATE runs SET personas=?, assignment=? WHERE id=?",
                           (dumps([new if name == old else name for name in personas]), dumps(assignment), row["id"]))
                moved["runs"] += 1
            moved["messages"] = db.execute(
                "UPDATE messages SET sender_name=? WHERE direction='out' AND lower(sender_name)=lower(?)",
                (display_name, old)).rowcount
        return moved

    def rename_engine(self, old: str, new: str) -> int:
        """Open runs of engine `old` are `new`'s (EngineSpec.formerly). Returns how many."""
        marks = ",".join("?" * len(OPEN))
        return self.db.execute(f"UPDATE runs SET engine=? WHERE engine=? AND status IN ({marks})",
                               (new, old, *OPEN)).rowcount

    def rename_module(self, old: str, new: str) -> int:
        """The stores of module `old` are module `new`'s (Extension.formerly); what `new` has wins. Returns how many
        entries moved."""
        moved = 0
        with self.db.transaction() as db:
            for row in db.query("SELECT DISTINCT scope FROM kv WHERE scope=? OR scope LIKE ?",
                                ("extension:" + old, "extension:" + old + " {%")):
                target = "extension:" + new + row["scope"][len("extension:" + old):]
                db.execute("INSERT OR IGNORE INTO kv(scope, key, value) SELECT ?, key, value FROM kv WHERE scope=?",
                           (target, row["scope"]))
                moved += db.execute("DELETE FROM kv WHERE scope=?", (row["scope"],)).rowcount
        return moved

    def record_message(self, address: str, direction: str, text: str, *, kind: str = "text",
                       sender_id: str = "", sender_name: str = "", message_id: str | None = None,
                       run_id: str | None = None, reply_to: str | None = None, quoted_text: str = "",
                       at: float | None = None, files=()) -> None:
        stored = dumps([dataclasses.asdict(file) for file in files]) if files else None
        self.db.execute(
            "INSERT INTO messages(address,direction,message_id,sender_id,sender_name,text,kind,run_id,reply_to,"
            "quoted_text,at,files) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (address, direction, message_id, sender_id, sender_name, text, kind, run_id, reply_to, quoted_text or "",
             at or time.time(), stored))

    def has_message(self, address: str, direction: str, message_id: str) -> bool:
        return self.db.one("SELECT 1 FROM messages WHERE address=? AND direction=? AND message_id=?",
                           (address, direction, message_id)) is not None

    def recent_messages(self, address: str, limit: int = 30) -> list[HistoryItem]:
        rows = self.db.query("SELECT * FROM messages WHERE address=? ORDER BY seq DESC LIMIT ?",
                             (address, max(1, min(int(limit), 200))))
        return [self._history_item(row) for row in reversed(rows)]

    def search_messages(self, address: str, text: str, limit: int = 20, before: str | None = None) -> list[HistoryItem]:
        """Messages of one conversation containing `text` in any case (Unicode case folding), newest first.
        `before`: an incoming message's ID; only messages recorded ahead of it (so not the question itself)."""
        bound = ("AND seq < COALESCE((SELECT MAX(seq) FROM messages WHERE address=? AND direction='in' "
                 "AND message_id=?), 9223372036854775807) ") if before else ""
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError, OverflowError):
            limit = 20
        parameters = (address, fold(text), *((address, before) if before else ()), limit)
        rows = self.db.query("SELECT * FROM messages WHERE address=? AND instr(casefold(text), ?) > 0 "
                             f"{bound}ORDER BY seq DESC LIMIT ?", parameters)
        return [self._history_item(row) for row in rows]

    @staticmethod
    def _history_item(row) -> HistoryItem:
        return HistoryItem(direction=row["direction"], text=row["text"], at=row["at"],
                           sender_name=row["sender_name"] or "", sender_id=row["sender_id"] or "",
                           kind=row["kind"], message_id=row["message_id"], reply_to=row["reply_to"],
                           quoted_text=row["quoted_text"] or "",
                           files=tuple(FileRef(**item) for item in loads(row["files"], [])))

    # ---- the log -----------------------------------------------------------------------------------
    @staticmethod
    def _entry(row) -> LogEntry:
        return LogEntry(row["id"], row["at"], row["module"], row["kind"], row["text"], row["address"], row["subject"],
                        row["persona"], loads(row["data"], {}))

    def add_log(self, address: str, module: str, kind: str, text: str, *, subject: str = "", persona: str = "",
                data: Mapping[str, Any] | None = None, keep: int = 2000) -> LogEntry:
        """One new line; beyond `keep` lines in this conversation, the oldest go."""
        with self.db.transaction() as db:
            cursor = db.execute("INSERT INTO log(at,module,kind,text,address,subject,persona,data) "
                                "VALUES(?,?,?,?,?,?,?,?)", (time.time(), module, kind, text, address, subject, persona,
                                                            dumps(dict(data or {}))))
            db.execute("DELETE FROM log WHERE address=? AND id NOT IN (SELECT id FROM log WHERE address=? ORDER BY id "
                       "DESC LIMIT ?)", (address, address, keep))
            return self._entry(db.one("SELECT * FROM log WHERE id=?", (cursor.lastrowid,)))

    def log_lines(self, address: str, limit: int = 20, offset: int = 0, *, kind: str = "", subject: str | None = None,
                  persona: str | None = None, terms: Sequence[str] = ()) -> list[LogEntry]:
        """This conversation's lines, newest first; `terms`: those holding any of these words."""
        sql, parameters = "SELECT * FROM log WHERE address=?", [address]
        for column, value in (("kind", kind or None), ("subject", subject), ("persona", persona)):
            if value is not None:
                sql, parameters = f"{sql} AND {column}=?", [*parameters, value]
        if terms:
            sql += " AND (" + " OR ".join("instr(lower(text), ?) > 0" for _ in terms) + ")"
            parameters += [term.lower() for term in terms]
        return [self._entry(row) for row in self.db.query(f"{sql} ORDER BY id DESC LIMIT ? OFFSET ?",
                                                          (*parameters, limit, offset))]

    def log_counts(self, address: str, subject: str | None = None, kind: str = "") -> dict[str, int]:
        """How many lines each persona has here (about `subject`, of `kind`, when given)."""
        sql, parameters = "SELECT persona, COUNT(*) AS n FROM log WHERE address=?", [address]
        for column, value in (("subject", subject), ("kind", kind or None)):
            if value is not None:
                sql, parameters = f"{sql} AND {column}=?", [*parameters, value]
        return {row["persona"]: row["n"] for row in self.db.query(f"{sql} GROUP BY persona ORDER BY persona",
                                                                  parameters)}

    def delete_log(self, address: str, subject: str | None = None, *, persona: str | None = None,
                   entry_id: int | None = None, kind: str = "") -> int:
        sql, parameters = "DELETE FROM log WHERE address=?", [address]
        for column, value in (("subject", subject), ("persona", persona), ("id", entry_id), ("kind", kind or None)):
            if value is not None:
                sql, parameters = f"{sql} AND {column}=?", [*parameters, value]
        return self.db.execute(sql, parameters).rowcount

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
                "status": row["status"], "result": loads(row["result"]), "finished_at": row["finished_at"]}

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
                "created_at": row["created_at"], "decided_at": row["decided_at"], "decided_by": row["decided_by"]}

    def approval(self, approval_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return None if row is None else self._approval(row)

    def approvals(self, status: str = "pending", run_id: str | None = None) -> list[dict]:
        sql, parameters = "SELECT * FROM approvals WHERE status=?", [status]
        if run_id is not None:
            sql, parameters = sql + " AND run_id=?", [*parameters, run_id]
        return [self._approval(row) for row in self.db.query(sql + " ORDER BY created_at", parameters)]

    def redact_approval(self, approval_id: str, arguments: dict) -> None:
        self.db.execute("UPDATE approvals SET arguments=? WHERE id=?", (dumps(arguments), approval_id))

    def action_names(self, run_id: str) -> set[str]:
        return {row["name"] for row in self.db.query("SELECT DISTINCT name FROM actions WHERE run_id=?", (run_id,))}

    def decide_approval(self, approval_id: str, status: str, decided_by: str) -> bool:
        """Atomically move a pending approval to approved/denied/expired. False if it was not pending."""
        cursor = self.db.execute(
            "UPDATE approvals SET status=?, decided_at=?, decided_by=? WHERE id=? AND status='pending'",
            (status, time.time(), decided_by, approval_id))
        return cursor.rowcount == 1

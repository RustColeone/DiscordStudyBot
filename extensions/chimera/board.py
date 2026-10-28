"""The board: Chimera's own record of tasks and their steps, shared by its engine, capabilities and page.

The core keeps each run durable (its state, events and approvals); the board is what people look at: every task
with its project, status and change set, every step with who did it, on which model, at what cost and how long it
took, and the page's transcript. It lives in the module's data folder (`workbench.db`). Whoever writes to it, the
page hears about it at once through `subscribe`, so it can show the work as it happens.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

OPEN = ("invited", "queued", "working", "review", "approved", "answered", "failed", "cancelled")
CLOSED = ("landed", "discarded")
AT_REST = ("failed", "cancelled", "landed", "discarded")  # no run works on the task any more

SCHEMA = """
create table if not exists tasks (id text primary key, project text not null, address text not null,
    title text not null, status text not null, phase text not null default '', workflow text not null default '',
    data text not null default '{}', created real not null, updated real not null);
create index if not exists tasks_address on tasks(address, created);
create table if not exists steps (task text not null, id text not null, seq integer not null, round integer not null,
    kind text not null, role text not null default '', seat text not null default '', status text not null,
    title text not null default '', data text not null default '{}', started real not null, ended real,
    primary key (task, id));
create table if not exists messages (id integer primary key autoincrement, task text not null, item text not null,
    at real not null);
create index if not exists messages_task on messages(task, id);
create table if not exists projects (name text primary key, settings text not null, created real not null);
create table if not exists kv (key text primary key, value text not null);
"""


class Board:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("pragma journal_mode=wal")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._seq = int(self._db.execute("select coalesce(max(seq), 0) from steps").fetchone()[0])
        self._listeners: list[Callable[[str, dict], None]] = []
        self.outcomes = self.path.with_name("outcomes.jsonl")
        for (task_id,) in self._db.execute(  # left open by a run stopped before steps were closed with it
                "select distinct task from steps where status = 'running' and task in (select id from tasks where "
                f"status in ({', '.join('?' * len(AT_REST))}))", AT_REST).fetchall():
            self._close_steps(task_id)

    def close(self) -> None:
        self._db.close()

    # ---- listeners ------------------------------------------------------------------------------
    def subscribe(self, listener: Callable[[str, dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _tell(self, event: str, payload: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, payload)
            except Exception:  # a page that cannot listen never stops the work
                pass

    # ---- tasks ----------------------------------------------------------------------------------
    @staticmethod
    def _task(row) -> dict | None:
        if row is None:
            return None
        task = dict(row)
        task["data"] = json.loads(task["data"])
        return task

    def task(self, task_id: str) -> dict | None:
        return self._task(self._db.execute("select * from tasks where id = ?", (task_id,)).fetchone())

    def tasks(self, limit: int = 300) -> list[dict]:
        rows = self._db.execute("select * from tasks order by created desc limit ?", (limit,)).fetchall()
        return [self._task(row) for row in rows]

    def readdress(self, old: str, new: str) -> int:
        """Tasks of interface `old` (`<old>:<task>`) are interface `new`'s: the page was renamed."""
        with self._lock:
            cursor = self._db.execute("update tasks set address = ? || substr(address, ?) where address like ?",
                                      (new + ":", len(old) + 2, old + ":%"))
            self._db.commit()
        return cursor.rowcount

    def latest(self, address: str) -> dict | None:
        """The conversation's newest task."""
        return self._task(self._db.execute("select * from tasks where address = ? order by created desc limit 1",
                                           (address,)).fetchone())

    def open_task(self, address: str) -> dict | None:
        """The conversation's newest task, unless it is closed (landed or discarded)."""
        task = self.latest(address)
        return task if task and task["status"] not in CLOSED else None

    def create_task(self, task_id: str, address: str, project: str, title: str, **data: Any) -> dict:
        now = time.time()
        with self._lock:
            self._db.execute("insert or ignore into tasks (id, project, address, title, status, data, created, updated) "
                             "values (?, ?, ?, ?, 'queued', ?, ?, ?)",
                             (task_id, project, address, title[:120] or "New task", json.dumps(data), now, now))
        task = self.task(task_id)
        self._tell("task", task)
        return task

    def update_task(self, task_id: str, *, data: dict | None = None, **fields: Any) -> dict | None:
        """Change columns (status, phase, title, workflow, project) and merge `data` key by key (None removes)."""
        with self._lock:
            row = self._db.execute("select data from tasks where id = ?", (task_id,)).fetchone()
            if row is None:
                return None
            merged = json.loads(row["data"])
            for key, value in (data or {}).items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            columns = {key: value for key, value in fields.items()
                       if key in ("status", "phase", "title", "workflow", "project")}
            if "title" in columns:
                columns["title"] = str(columns["title"])[:120]
            assignments = ", ".join(f"{key} = ?" for key in columns)
            self._db.execute(f"update tasks set {assignments + ', ' if assignments else ''}data = ?, updated = ? "
                             "where id = ?", (*columns.values(), json.dumps(merged), time.time(), task_id))
        task = self.task(task_id)
        self._tell("task", task)
        if columns.get("status") in AT_REST:
            self._close_steps(task_id)
        return task

    def _close_steps(self, task_id: str) -> None:
        """A task at rest has no step still running: one its stopped run left open (a turn cut off by `$cancel`
        or a failure, an approval nobody will answer) ends as `skipped`, so no page or client waits on it."""
        for step in self.steps(task_id):
            if step["status"] == "running":
                self.put_step(task_id, step["id"], status="skipped")

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            for table, key in (("tasks", "id"), ("steps", "task"), ("messages", "task")):
                self._db.execute(f"delete from {table} where {key} = ?", (task_id,))
        self._tell("deleted", {"id": task_id})

    def search(self, query: str = "", project: str = "", limit: int = 10) -> list[dict]:
        """The Classroom's logbook: tasks whose title, request or summary hold every word of `query` (any case), newest
        first; optionally of one project."""
        words = [word.lower() for word in str(query or "").split() if word.strip()]
        found = []
        for task in self.tasks(1000):
            if project and task["project"] != project:
                continue
            data = task["data"]
            text = " ".join(str(item or "") for item in (task["title"], data.get("request"), data.get("summary"),
                                                         data.get("answer"))).lower()
            if all(word in text for word in words):
                found.append(task)
            if len(found) >= limit:
                break
        return found

    def notebook(self, project: str, before: str = "", limit: int = 5) -> list[dict]:
        """What the club did in a project lately (finished tasks), for the Classroom's own memory of it."""
        rows = [task for task in self.tasks(300) if task["project"] == project and task["id"] != before
                and task["status"] in ("landed", "answered", "discarded", "failed")]
        return rows[:limit]

    # ---- steps ----------------------------------------------------------------------------------
    @staticmethod
    def _step(row) -> dict:
        step = dict(row)
        step["data"] = json.loads(step["data"])
        return step

    def put_step(self, task_id: str, step_id: str, *, data: dict | None = None, **fields: Any) -> dict:
        """Create or change a step (by its ID within the task); `data` merges key by key. The first write
        decides its place in the order."""
        now = time.time()
        with self._lock:
            row = self._db.execute("select * from steps where task = ? and id = ?", (task_id, step_id)).fetchone()
            if row is None:
                self._seq += 1
                self._db.execute(
                    "insert into steps (task, id, seq, round, kind, role, seat, status, title, data, started, ended) "
                    "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, step_id, self._seq, int(fields.get("round", 1)), fields.get("kind", "note"),
                     fields.get("role", ""), fields.get("seat", ""), fields.get("status", "running"),
                     fields.get("title", ""), json.dumps(data or {}), fields.get("started", now), fields.get("ended")))
            else:
                merged = json.loads(row["data"])
                merged.update(data or {})
                columns = {key: value for key, value in fields.items()
                           if key in ("round", "kind", "role", "seat", "status", "title", "ended")}
                if columns.get("status") in ("done", "failed", "skipped") and "ended" not in columns and not row["ended"]:
                    columns["ended"] = now
                assignments = ", ".join(f"{key} = ?" for key in columns)
                self._db.execute(f"update steps set {assignments + ', ' if assignments else ''}data = ? "
                                 "where task = ? and id = ?", (*columns.values(), json.dumps(merged), task_id, step_id))
            step = self._step(self._db.execute("select * from steps where task = ? and id = ?",
                                               (task_id, step_id)).fetchone())
        self._tell("step", step)
        return step

    def steps(self, task_id: str) -> list[dict]:
        return [self._step(row) for row in self._db.execute("select * from steps where task = ? order by seq",
                                                            (task_id,)).fetchall()]

    def totals(self, task_id: str) -> dict:
        """What the task's model calls cost so far: calls, tokens, dollars (when prices are known) and seconds."""
        calls = tokens_in = tokens_out = 0
        cost, priced, seconds = 0.0, True, 0.0
        for step in self.steps(task_id):
            for call in step["data"].get("calls", ()):
                calls += 1
                tokens_in += int(call.get("input_tokens") or 0)
                tokens_out += int(call.get("output_tokens") or 0)
                seconds += float(call.get("seconds") or 0)
                if call.get("cost") is None:
                    priced = False
                else:
                    cost += float(call["cost"])
        return {"calls": calls, "input_tokens": tokens_in, "output_tokens": tokens_out,
                "cost": round(cost, 6) if calls and priced else None, "partial_cost": round(cost, 6),
                "model_seconds": round(seconds, 1)}

    # ---- the page's transcript ------------------------------------------------------------------
    def add_message(self, task_id: str, item: dict) -> dict:
        with self._lock:
            cursor = self._db.execute("insert into messages (task, item, at) values (?, ?, ?)",
                                      (task_id, json.dumps(item, ensure_ascii=False), item.get("at", time.time())))
            item = {**item, "id": cursor.lastrowid, "task": task_id}
            self._db.execute("update messages set item = ? where id = ?", (json.dumps(item, ensure_ascii=False),
                                                                           cursor.lastrowid))
        self._tell("message", item)
        return item

    def update_message(self, message_id: int, item: dict) -> None:
        with self._lock:
            self._db.execute("update messages set item = ? where id = ?", (json.dumps(item, ensure_ascii=False),
                                                                           message_id))
        self._tell("message_update", item)

    def messages(self, task_id: str, after: int = 0) -> list[dict]:
        rows = self._db.execute("select item from messages where task = ? and id > ? order by id", (task_id, after))
        return [json.loads(row["item"]) for row in rows.fetchall()]

    def find_messages(self, predicate: Callable[[dict], bool], limit: int = 2000) -> list[dict]:
        rows = self._db.execute("select item from messages order by id desc limit ?", (limit,)).fetchall()
        return [item for item in (json.loads(row["item"]) for row in rows) if predicate(item)]

    # ---- projects added in the page -------------------------------------------------------------
    def added_projects(self) -> dict[str, dict]:
        return {row["name"]: json.loads(row["settings"])
                for row in self._db.execute("select * from projects order by created").fetchall()}

    def add_project(self, name: str, settings: dict) -> None:
        with self._lock:
            self._db.execute("insert or replace into projects (name, settings, created) values (?, ?, ?)",
                             (name, json.dumps(settings), time.time()))
        self._tell("projects", {})

    def remove_project(self, name: str) -> None:
        with self._lock:
            self._db.execute("delete from projects where name = ?", (name,))
        self._tell("projects", {})

    # ---- small facts the module keeps (who has a key, as the engine last saw) ----------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("select value from kv where key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("insert or replace into kv (key, value) values (?, ?)", (key, json.dumps(value)))
        self._tell("kv", {"key": key})

    # ---- outcomes, for tuning the routing later -------------------------------------------------
    def record_outcome(self, task_id: str, event: str, **fields: Any) -> None:
        """One line per routing decision and per result (validated, reviewed, landed, discarded) in
        outcomes.jsonl: the evidence to tune the router on later."""
        task = self.task(task_id) or {}
        line = {"at": round(time.time(), 3), "task": task_id, "event": event, "project": task.get("project"),
                "workflow": task.get("workflow"), **fields}
        with self._lock, self.outcomes.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")

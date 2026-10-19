"""The web UI's own record of what it showed: conversations and their messages, in one SQLite file.

The core keeps the real history (the tank); this is the page's transcript, so a reload shows the same
bubbles, buttons and files that were delivered.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL,
                                          updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL,
                                     at REAL NOT NULL, approval TEXT, item TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation, id);
CREATE INDEX IF NOT EXISTS messages_by_approval ON messages(approval);
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
                                  size INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(conversations)")}
            if "position" not in columns:
                # Chats are kept in the order the user arranges them (pinned first); start from most recent first.
                self._db.execute("ALTER TABLE conversations ADD COLUMN position REAL")
                self._db.execute("ALTER TABLE conversations ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
                self._db.execute("UPDATE conversations SET position = -updated_at")
            if "personas" not in columns:
                # Who is in each chat (a JSON list of persona names); NULL: the web UI's whole roster.
                self._db.execute("ALTER TABLE conversations ADD COLUMN personas TEXT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- conversations ----------------------------------------------------------------------------
    @staticmethod
    def _row(row) -> dict[str, Any]:
        value = dict(row)
        value["personas"] = json.loads(value["personas"]) if value.get("personas") else None
        return value

    def conversations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM conversations ORDER BY pinned DESC, position, created_at DESC"
                                    ).fetchall()
        return [self._row(row) for row in rows]

    def conversation(self, conversation: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM conversations WHERE id=?", (conversation,)).fetchone()
        return self._row(row) if row else None

    def ensure(self, conversation: str, title: str = "", personas: list[str] | None = None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            top = self._db.execute("SELECT MIN(position) FROM conversations").fetchone()[0]
            self._db.execute("INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at, position, personas) "
                             "VALUES(?,?,?,?,?,?)", (conversation, title or conversation, now, now,
                                                     (top if top is not None else 0) - 1,
                                                     json.dumps(personas) if personas else None))
            row = self._db.execute("SELECT * FROM conversations WHERE id=?", (conversation,)).fetchone()
        return self._row(row)

    def rename(self, conversation: str, title: str) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation))

    def pin(self, conversation: str, pinned: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE conversations SET pinned=? WHERE id=?", (int(pinned), conversation))

    def order(self, conversations: list[str]) -> None:
        """Arrange chats in this order (unknown IDs are ignored; chats not listed keep their place after)."""
        with self._lock:
            for index, conversation in enumerate(conversations):
                self._db.execute("UPDATE conversations SET position=? WHERE id=?", (index, conversation))

    def delete(self, conversation: str) -> list[Path]:
        """Remove a chat and its transcript; returns the files only it showed, for the caller to remove."""
        with self._lock:
            rows = self._db.execute("SELECT item FROM messages WHERE conversation=?", (conversation,)).fetchall()
            ids = set()
            for row in rows:
                item = json.loads(row["item"])
                ids.update(file["id"] for file in [*(item.get("files") or []), item.get("file") or {}]
                           if file.get("id"))
            paths = []
            for file_id in ids:
                record = self._db.execute("SELECT path FROM files WHERE id=?", (file_id,)).fetchone()
                if record is not None:
                    paths.append(Path(record["path"]))
                self._db.execute("DELETE FROM files WHERE id=?", (file_id,))
            self._db.execute("DELETE FROM messages WHERE conversation=?", (conversation,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (conversation,))
        return paths

    # ---- messages ---------------------------------------------------------------------------------
    def add(self, conversation: str, item: dict[str, Any]) -> dict[str, Any]:
        """Store one message (a dict the page renders) and return it with its `id`."""
        now = time.time()
        item = {**item, "conversation": conversation, "at": item.get("at") or now}
        approval = (item.get("data") or {}).get("approval_id") if item.get("code") == "approval_request" else None
        with self._lock:
            cursor = self._db.execute("INSERT INTO messages(conversation, at, approval, item) VALUES(?,?,?,?)",
                                      (conversation, item["at"], approval, json.dumps(item, ensure_ascii=False)))
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation))
        return {**item, "id": cursor.lastrowid}

    def messages(self, conversation: str, after: int = 0, limit: int = 300) -> list[dict[str, Any]]:
        with self._lock:
            if after:
                rows = self._db.execute("SELECT id, item FROM messages WHERE conversation=? AND id>? ORDER BY id "
                                        "LIMIT ?", (conversation, after, limit)).fetchall()
            else:
                rows = self._db.execute("SELECT id, item FROM messages WHERE conversation=? ORDER BY id DESC LIMIT ?",
                                        (conversation, limit)).fetchall()[::-1]
        return [{**json.loads(row["item"]), "id": row["id"]} for row in rows]

    def decide(self, approval_id: str, decision: str) -> dict[str, Any] | None:
        """Mark the card showing an approval as decided (its buttons go away); returns the updated card."""
        with self._lock:
            row = self._db.execute("SELECT id, item FROM messages WHERE approval=? ORDER BY id DESC LIMIT 1",
                                   (approval_id,)).fetchone()
            if row is None:
                return None
            item = {**json.loads(row["item"]), "decided": decision}
            self._db.execute("UPDATE messages SET item=? WHERE id=?",
                             (json.dumps(item, ensure_ascii=False), row["id"]))
        return {**item, "id": row["id"]}

    # ---- files --------------------------------------------------------------------------------------
    def add_file(self, file_id: str, path: Path, name: str, mime: str, size: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO files(id, path, name, mime, size) VALUES(?,?,?,?,?)",
                             (file_id, str(path), name, mime, size))

    def file(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return dict(row) if row else None

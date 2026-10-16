"""Persona diaries: notes a persona keeps about one person in one conversation.

The core owns the rules so every engine keeps diaries the same way: notes are short paraphrases with a
key, kind and importance; a run can write only a few; they are only about the person who triggered the
run; and that person can read or delete them with `$memory`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from contracts import DIARY_KINDS, DiaryNote

if TYPE_CHECKING:
    from core.manager.manager import Manager
    from core.storage.tank import RunRecord

MAX_NOTE_CHARS = 400
WRITES_PER_RUN = 4
NOTES_PER_PERSON = 200
KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
IGNORED_TERMS = {"the", "that", "this", "you", "your", "remember", "what", "about", "with", "have",
                 "你们", "你們", "主人", "记得", "記得", "觉得", "覺得", "什么", "什麼"}


def search_terms(text: str) -> list[str]:
    """Latin words of 3+ letters and overlapping two-character pieces of CJK runs."""
    terms: list[str] = []
    for token in re.findall(r"[a-zA-Z]{3,}|[\u4e00-\u9fff]{2,}", text):
        pieces = [token.lower()] if token.isascii() else [token[i:i + 2] for i in range(min(len(token) - 1, 60))]
        for piece in pieces:
            if piece not in IGNORED_TERMS and piece not in terms:
                terms.append(piece)
    return terms[:24]


def normalized(text: str) -> str:
    return " ".join(text.split())


class DiaryHandle:
    """Implements contracts.Diary for one run and one of its personas."""

    def __init__(self, manager: "Manager", record: "RunRecord", persona: str, trigger_text: str, subject: str):
        self._manager, self._run_id, self.persona = manager, record.id, persona
        self._address, self._subject = record.address, subject
        self._source = normalized(trigger_text)
        self.available = bool(subject) and record.origin != "routine"

    def recall(self, text: str = "", limit: int = 8) -> list[DiaryNote]:
        if not self.available:
            return []
        limit = max(1, min(int(limit), 50))
        tank = self._manager.tank
        terms = search_terms(text)
        relevant = tank.notes(self.persona, self._address, self._subject, limit, terms=terms) if terms else []
        seen = {note.id for note in relevant}
        rest = [note for note in tank.notes(self.persona, self._address, self._subject, limit) if note.id not in seen]
        return [*relevant, *rest][:limit]

    def write(self, key: str, kind: str, note: str, importance: int) -> bool:
        if not self.available:
            raise ValueError("This run has no person to keep notes about")
        text = normalized(str(note))
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise ValueError("Diary keys are 1-80 lowercase letters, digits, dots, underscores or hyphens")
        if kind not in DIARY_KINDS:
            raise ValueError(f"Diary kind must be one of {list(DIARY_KINDS)}")
        if not text or len(text) > MAX_NOTE_CHARS:
            raise ValueError(f"A diary note needs 1-{MAX_NOTE_CHARS} characters")
        if type(importance) is not int or not 1 <= importance <= 5:
            raise ValueError("Diary importance is an integer from 1 to 5")
        if text == self._source or (len(self._source) >= 60 and self._source in text):
            raise ValueError("Diary notes must paraphrase, not copy the message")
        record = self._manager.tank.run(self._run_id)
        if record.usage.get("diary_writes", 0) >= WRITES_PER_RUN:
            current = self._manager.tank.note(self.persona, self._address, self._subject, key)
            if current is not None and (current.kind, current.note, current.importance) == (kind, text, importance):
                return False
            raise ValueError(f"A run can write at most {WRITES_PER_RUN} diary notes")
        changed = self._manager.tank.save_note(self.persona, self._address, self._subject, key, kind, text,
                                               importance, NOTES_PER_PERSON)
        if changed:
            self._manager._add_usage(self._run_id, diary_writes=1)
            self._manager.audit.record("diary_write", run=self._run_id, persona=self.persona, key=key)
        return changed

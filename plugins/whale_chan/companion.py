"""鲸酱's memory and mood, kept by mneme-llm: a standalone package with one SQLite file any model can read.

Swap her model and the file carries over: what she knows about each person, how it changed, and how she
feels toward them. Without mneme-llm installed (`pip install mneme-llm`), or with `memory: {engine: core}`,
the chat engine keeps using the core's diary instead.

    before a reply   her notes for the message (word matching, plus meaning: OpenAI embeddings → word centroids
                     learned from them → her own model unifying the language), and her mood's news, told once
    after a reply    one decision request judges her mood and asks whether there is anything worth keeping
                     (GPT-6 Luna → the micro model → her own model); only then does a note writer run; once a
                     day per person she rewrites her short impression of them
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

try:
    import mneme_llm
    from mneme_llm import MoodSettings
except ImportError:  # the package is optional; the engine falls back to the core diary
    mneme_llm = None

KEYED = {"fact", "preference", "plan"}  # an episode's key would make the next episode replace it
DAY = 86400


def available() -> bool:
    return mneme_llm is not None


class Companion:
    def __init__(self, path: Path, config: Mapping[str, Any] | None = None, *, name: str = "鲸酱"):
        if mneme_llm is None:
            raise RuntimeError("mneme-llm is not installed")
        memory = dict((config or {}).get("memory") or {})
        mood = dict((config or {}).get("mood") or {})
        self.prefix = str(memory.get("subject_prefix", "discord"))
        self.capture = memory.get("capture", True) is not False
        self.reflect_after = int(memory.get("reflect_after_notes", 5))
        self.writer_profile = memory.get("writer_profile") or None  # a fixed model for notes, whatever she chats on
        self.judge_mood = mood.get("enabled", True) is not False
        settings = MoodSettings(baseline=float(mood.get("baseline", 70)), floor=float(mood.get("floor", 0)),
                                tell=str(mood.get("tell", "event")),
                                threshold=float(mood.get("threshold", 0.6)))
        language = memory.get("language", "English")
        self.m = mneme_llm.Mneme(
            str(path), str(memory.get("space", "whale_chan")), display_language=str(memory.get("lang", "en")),
            companion_name=name,
            luna=mood.get("luna", True) is not False, micro=mood.get("micro", True) is not False,
            embed=str(memory.get("embeddings", "openai")) == "openai",
            meaning_micro=memory.get("meaning_micro", True) is not False,
            memory_language=str(language) if language else None, mood=settings,
            daily_quota=int(memory.get("daily_quota", 8)))

    def subject(self, sender_id: str) -> str:
        return f"{self.prefix}:{sender_id}"

    # ---- before a reply ---------------------------------------------------------------------------------
    async def block(self, subject: str, query: str, person: str, complete=None) -> str:
        """Her notes for this message (marked as data, not instructions) and her mood's news, told once."""
        notes = await self.m.memory.acontext(subject, query, user_name=person, complete=complete)
        news = self.m.mood.news(subject) if self.judge_mood else None
        return "\n\n".join(part for part in (notes or f"Notes about {person}: (none yet)", news) if part)

    def adopt(self, subject: str, notes) -> int:
        """Bring the core diary's notes of a person over, once (later calls do nothing). `notes` may be a
        function returning them, so they are only read the first time."""
        flag = f"adopted:{self.m.space}:{subject}"
        if self.m.store.meta(flag):
            return 0
        count = 0
        for note in notes() if callable(notes) else notes:
            kind = note.kind if note.kind in mneme_llm.KINDS else "episode"
            try:
                if self.m.memory.remember(subject, note.note, kind=kind, key=note.key if kind in KEYED else None,
                                          importance=note.importance, source="core-diary") is not None:
                    count += 1
            except ValueError:
                continue
        self.m.store.set_meta(flag, str(time.time()))
        return count

    # ---- after a reply ----------------------------------------------------------------------------------
    async def observe(self, subject: str, exchange, *, source: str, person: str, complete=None):
        """Her mood, and the notes worth keeping, from one exchange (one decision request for both)."""
        if not self.judge_mood and not self.capture:
            return None
        return await self.m.aobserve(subject, exchange, source=source, user_name=person, complete=complete,
                                     mood=self.judge_mood, capture=self.capture)

    async def reflect_if_due(self, subject: str, *, person: str, complete=None) -> str | None:
        """Rewrite her impression of the person at most once a day, once there are a few notes to go on."""
        updated = self.m.memory.profile_updated_at(subject)
        if updated is not None and time.time() - updated < DAY:
            return None
        if len(self.m.memory.notes(subject)) < self.reflect_after:
            return None
        return await self.m.areflect(subject, user_name=person, complete=complete)

    # ---- for speaking up --------------------------------------------------------------------------------
    def mood(self, subject: str) -> str:
        return self.m.mood.describe(subject)

    def feeling(self, subject: str) -> tuple[str, float] | None:
        strongest = self.m.mood.state(subject).strongest(1, above=20)
        return strongest[0] if strongest else None

    def upcoming(self, subject: str, now: float, days: float = 2.0) -> list:
        """Plans dated from yesterday to `days` ahead, soonest first: things to ask about."""
        plans = [note for note in self.m.memory.notes(subject, kinds=["plan"])
                 if now - DAY <= note.happened_at <= now + days * DAY]
        return sorted(plans, key=lambda note: note.happened_at)

    def close(self) -> None:
        self.m.close()

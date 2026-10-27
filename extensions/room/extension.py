"""Room extension: the shared multi-character room engine. Its characters are the students (extensions/students),
or any personas an assignment names."""

from __future__ import annotations

from contracts import Contributions, Extension, Setting

from .engine import RoomEngine, flag, number


def setup(context) -> Contributions:
    config = context.config
    engine = RoomEngine(max_steps=number(config.get("max_steps"), 24, 4, 50),
                        history_limit=number(config.get("history_limit"), 30, 1, 150),
                        max_tokens=number(config.get("max_tokens"), 1500, 100, 32000),
                        aliases=config.get("aliases") or {},
                        chime_in=flag(config.get("chime_in"), False), compact=flag(config.get("compact"), True),
                        compact_every=number(config.get("compact_every"), 20, 1, 1000),
                        compact_profile=str(config.get("compact_profile") or "default"),
                        diary=flag(config.get("diary"), False))
    moved = [key for key in ("providers", "profiles") if key in config]
    if moved:  # the characters' models moved to the students module (2026-10-27)
        context.log("settings_moved", keys=moved, to="extensions/students/config.yml")
    return Contributions(engines=[engine])


EXTENSION = Extension("room", setup, contracts="1", version="1",
                      description="Shared room: several personas work one request together", settings=(
    Setting("max_steps", "Most work-board steps per request", "number", 24),
    Setting("history_limit", "Earlier messages every character reads word for word", "number", 30),
    Setting("chime_in", "A group message naming no one: whose turn it is answers, the others may join in", "boolean",
            False),
    Setting("compact", "Fold messages older than history_limit into the chat's rolling summary", "boolean", True),
    Setting("diary", "Each character keeps notes about people in the core diary (off: the companion module keeps "
            "her memory)", "boolean", False),
))

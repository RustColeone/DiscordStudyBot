"""Room extension: the shared multi-character room engine and its four default characters."""

from __future__ import annotations

from contracts import Contributions, Extension

from .engine import RoomEngine
from .personas import PERSONAS


def setup(context) -> Contributions:
    config = context.config
    engine = RoomEngine(max_steps=max(4, min(int(config.get("max_steps", 24)), 50)),
                        history_limit=int(config.get("history_limit", 16)),
                        max_tokens=int(config.get("max_tokens", 1500)),
                        aliases=config.get("aliases") or {"claude": ["Haiku"]})
    return Contributions(engines=[engine], personas=PERSONAS)


EXTENSION = Extension("room", setup, contracts="1", version="1",
                      description="Shared room: several personas work one request together")

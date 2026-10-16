"""Built-in reference engine extension."""

from contracts import Contributions, Extension

from .engine import AssistantEngine


def setup(context) -> Contributions:
    config = context.config
    return Contributions(engines=[AssistantEngine(max_steps=int(config.get("max_steps", 8)),
                                                  history_limit=int(config.get("history_limit", 16)),
                                                  effort=str(config.get("effort", "low")))])


EXTENSION = Extension("assistant", setup, contracts="1", version="1",
                      description="Single-persona tool-using engine (reference implementation)")

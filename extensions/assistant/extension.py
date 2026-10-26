"""Built-in reference engine extension."""

from contracts import Contributions, Extension, Setting

from .engine import AssistantEngine


def setup(context) -> Contributions:
    config = context.config
    return Contributions(engines=[AssistantEngine(max_steps=int(config.get("max_steps", 8)),
                                                  history_limit=int(config.get("history_limit", 16)),
                                                  effort=str(config.get("effort", "low")),
                                                  tool_calls=config.get("tool_calls", True) is not False)])


EXTENSION = Extension("assistant", setup, contracts="1", version="1",
                      description="Single-persona tool-using engine (reference implementation)", settings=(
    Setting("max_steps", "Most tool rounds per request", "number", 8),
    Setting("history_limit", "Earlier messages the assistant reads", "number", 16),
    Setting("effort", "Reasoning effort: low, medium or high", "text", "low"),
    Setting("tool_calls", "Let models that can call tools do so natively (false: JSON turns only)", "boolean", True),
))

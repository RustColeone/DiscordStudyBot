"""Provider-independent wire format for a room turn; tool arguments are validated separately."""
from contextlib import contextmanager
from contextvars import ContextVar


structured_room = ContextVar("structured_room", default=False)


def _object(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


ROOM_OUTPUT_SCHEMA = _object({
    "status": {"type": "string", "enum": ["continue", "complete", "needs_input", "blocked"]},
    "message": {"type": "string", "description": "Your short spoken reply in character and the user's language."},
    "work_note": {"type": "string", "description": "A short public explanation spoken as your character to the user, at most 480 characters. Empty if casual or redundant with message; never a detached status report."},
    "actions": {"type": "array", "items": _object({
        "tool": {"type": "string", "description": "Exact registered tool name."},
        "arguments": {"type": "string", "description": "JSON-encoded object of actual tool input values; use {} for no arguments."},
    })},
    "jobs": {"type": "array", "items": _object({
        "id": {"type": "string"},
        "character": {"type": "string", "enum": ["DeepSeek", "Luna", "Gemini", "Claude"]},
        "instruction": {"type": "string"},
        "after": {"type": "array", "items": {"type": "string"}},
    })},
    "diary_updates": {"type": "array", "description": "Your private selective diary updates, at most two. Usually empty. Never a transcript.",
                      "items": _object({"key": {"type": "string"},
                                        "kind": {"type": "string", "enum": ["fact", "episode", "reflection"]},
                                        "note": {"type": "string"}, "importance": {"type": "integer"}})},
})


@contextmanager
def room_output_mode():
    """Context-local so concurrent roleplay and tool calls keep their own output formats."""
    token = structured_room.set(True)
    try:
        yield
    finally:
        structured_room.reset(token)

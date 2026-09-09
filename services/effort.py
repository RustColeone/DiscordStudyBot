from typing import Dict


EFFORT_PROFILES: Dict[str, Dict[str, object]] = {
    "low": {
        "max_tokens": 500,
        "deepseek_max_tokens": 500,
        "planning_attempts": 1,
        "instruction": "Respond directly and use the minimum reasoning needed.",
    },
    "medium": {
        "max_tokens": 1000,
        "deepseek_max_tokens": 16000,
        "planning_attempts": 2,
        "instruction": "Reason carefully, resolve ambiguity from context, and verify the response before answering.",
    },
    "high": {
        "max_tokens": 2000,
        "deepseek_max_tokens": 384000,
        "planning_attempts": 3,
        "instruction": (
            "Use thorough reasoning. Infer intent across languages, inspect all available tools, "
            "check arguments and ordering, and verify the result before answering."
        ),
    },
}


def normalize_effort(value: str) -> str:
    normalized = str(value or "").lower()
    return normalized if normalized in EFFORT_PROFILES else "high"


def max_tokens_for_effort(value: str) -> int:
    return int(EFFORT_PROFILES[normalize_effort(value)]["max_tokens"])


def deepseek_max_tokens_for_effort(value: str) -> int:
    return int(EFFORT_PROFILES[normalize_effort(value)]["deepseek_max_tokens"])


def planning_attempts_for_effort(value: str) -> int:
    return int(EFFORT_PROFILES[normalize_effort(value)]["planning_attempts"])


def instruction_for_effort(value: str) -> str:
    return str(EFFORT_PROFILES[normalize_effort(value)]["instruction"])
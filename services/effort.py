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
    "max": {
        "max_tokens": 4000,
        "deepseek_max_tokens": 384000,
        "planning_attempts": 3,
        "instruction": (
            "Use maximum available reasoning. Resolve ambiguity from context, inspect all available tools, "
            "check arguments and ordering, test competing interpretations, and verify the result before answering."
        ),
    },
}

RESPONSE_DEPTH_PROFILES: Dict[str, Dict[str, object]] = {
    "concise": {
        "max_tokens": 1000,
        "instruction": "Keep the answer deliberately concise while still completing the request.",
    },
    "standard": {
        "max_tokens": 3000,
        "instruction": "Use the natural amount of detail needed for a complete ordinary answer.",
    },
    "detailed": {
        "max_tokens": 8000,
        "instruction": (
            "Develop the answer thoroughly with the structure, explanation, examples, and edge cases "
            "needed by the request. Do not compress it into a few sentences."
        ),
    },
    "exhaustive": {
        "max_tokens": 16000,
        "instruction": (
            "Provide the explicitly requested comprehensive treatment. Preserve useful detail and organize "
            "the response for navigation rather than shortening it to fit an ordinary chat reply."
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


def normalize_response_depth(value: str) -> str:
    normalized = str(value or "").lower()
    return normalized if normalized in RESPONSE_DEPTH_PROFILES else "standard"


def max_tokens_for_response_depth(value: str) -> int:
    return int(RESPONSE_DEPTH_PROFILES[normalize_response_depth(value)]["max_tokens"])


def instruction_for_response_depth(value: str) -> str:
    return str(RESPONSE_DEPTH_PROFILES[normalize_response_depth(value)]["instruction"])
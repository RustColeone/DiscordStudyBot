"""Built-in configuration, so a fresh clone runs with only a Discord token and a model key.

`core.yml` (optional) is merged over these values: mappings merge key by key, lists and scalars
replace. Relative paths here are relative to the repository root.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_PROMPT = ("You are a helpful, friendly assistant in a Discord server. Answer concisely in the language "
                  "the person writes in. Use tools when they help, and say plainly when you cannot do something.")


def default_config() -> dict:
    owner = (os.getenv("DISCORD_OWNER_ID") or os.getenv("CREATOR_USER_ID") or "").strip()
    config = {
        "data_dir": str(ROOT / "data" / "core"),
        "extensions": [str(ROOT / "extensions" / name) for name in ("discord", "assistant", "basic")],
        "extension_config": {},
        "providers": {
            "deepseek": {"kind": "openai_compatible", "base_url": "https://api.deepseek.com",
                         "api_key_env": "DEEPSEEK_API_KEY", "schema_mode": "json_object", "effort_param": True},
            "openai": {"kind": "openai_compatible", "api_key_env": "OPENAI_API_KEY", "schema_mode": "native",
                       "token_param": "max_completion_tokens", "effort_param": True},
            "gemini": {"kind": "gemini", "api_key_env": "GEMINI_API_KEY"},
            "claude": {"kind": "anthropic", "api_key_env": "CLAUDE_API_KEY", "adaptive_thinking": True},
            "openrouter": {"kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
                           "api_key_env": "OPENROUTER_API_KEY", "schema_mode": "json_object"},
        },
        # Candidates without an API key are skipped, so any one configured provider works.
        "profiles": {"default": [
            {"provider": "deepseek", "model": "deepseek-flash"},
            {"provider": "openai", "model": "gpt-6-luna"},
            {"provider": "gemini", "model": "gemini-3.8-flash"},
            {"provider": "claude", "model": "claude-haiku-5-5"},
        ]},
        "personas": {"assistant": {"display_name": "Assistant", "profile": "default", "prompt": DEFAULT_PROMPT}},
        "assignments": {
            "*": {"engine": "assistant", "personas": ["assistant"]},
            "routine:*": {"engine": "assistant", "personas": ["assistant"], "allow": ["basic.*"],
                          "budget": {"model_calls": 8, "tool_calls": 8, "seconds": 300}},
        },
        "admission": {"*": {"enabled": True, "require_mention_in_groups": True}},
        "routes": {},
        "routines": {},
    }
    if owner:
        config["owners"] = {"discord": [owner]}
        config["control"] = f"discord:dm:{owner}"
    return config


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result

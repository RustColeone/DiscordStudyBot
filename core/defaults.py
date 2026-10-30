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


# Basic features that only affect the requester's own conversation (or its server's voice channel), so
# anyone may use them without the owner's approval.
BASIC_FEATURES = ["reminders.create", "reminders.cancel", "discord.music", "discord.music_control",
                  "discord.queue_music", "discord.clock_start", "discord.clock_stop", "core.add_routine",
                  "core.delete_routine", "core.pause_routine", "core.resume_routine"]
# What $model offers to choose from: language models released since mid-2026 (models_since uses the release
# dates OpenAI, xAI and Anthropic give; Gemini gives none, so its list is narrowed by name only).
MODELS_SINCE = "2026-06-01"
NOT_LANGUAGE = ["image", "realtime", "live", "transcribe", "audio", "tts", "whisper", "embedding", "moderation",
                "search"]
DEFAULT_CHAIN = [
    {"provider": "deepseek", "model": "deepseek-flash"},
    {"provider": "openai", "model": "gpt-6-luna"},
    {"provider": "gemini", "model": "gemini-3.8-flash"},
    {"provider": "claude", "model": "claude-haiku-5-5"},
]


def default_config() -> dict:
    owner = (os.getenv("DISCORD_OWNER_ID") or os.getenv("CREATOR_USER_ID") or "").strip()
    config = {
        "data_dir": str(ROOT / "data" / "core"),
        # Every module folder (one with an extension.py) loads: the built-in ones in extensions/, then
        # the external ones dropped into plugins/. core.yml turns some off with `modules: {exclude: [...]}`.
        "modules": {"dirs": [str(ROOT / "extensions"), str(ROOT / "plugins")]},
        "extension_config": {},
        "providers": {
            # inline_history: its JSON mode answers blank after an assistant turn (see adapters.inline_history).
            "deepseek": {"kind": "openai_compatible", "base_url": "https://api.deepseek.com",
                         "api_key_env": "DEEPSEEK_API_KEY", "schema_mode": "json_object", "effort_param": True,
                         "inline_history": True},
            "openai": {"kind": "openai_compatible", "api_key_env": "OPENAI_API_KEY", "schema_mode": "native",
                       "token_param": "max_completion_tokens", "effort_param": True,
                       "models_since": MODELS_SINCE, "models_exclude": NOT_LANGUAGE},
            # Legacy spacing between Gemini requests avoids its per-minute rate limits.
            "gemini": {"kind": "gemini", "api_key_env": "GEMINI_API_KEY", "min_interval_s": 1.1,
                       "models_exclude": [*NOT_LANGUAGE, "lyria", "robotics", "computer-use", "deep-research",
                                          "antigravity", "nano-banana"]},
            "claude": {"kind": "anthropic", "api_key_env": "CLAUDE_API_KEY", "adaptive_thinking": True,
                       "models_since": MODELS_SINCE},
            "openrouter": {"kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
                           "api_key_env": "OPENROUTER_API_KEY", "schema_mode": "json_object", "vision": False,
                           "effort_param": True},
        },
        # Candidates without an API key are skipped, so any one configured provider works. Modules ship their
        # own profiles (the room: one per character).
        "profiles": {"default": DEFAULT_CHAIN},
        "personas": {"assistant": {"display_name": "Assistant", "profile": "default", "prompt": DEFAULT_PROMPT}},
        # The core's own agent, answering in MomoChat (extensions/momochat) wherever nothing else is assigned.
        "assignments": {
            "*": {"engine": "momochat", "personas": ["assistant"]},
            "routine:*": {"engine": "momochat", "personas": ["assistant"],
                          "budget": {"model_calls": 8, "tool_calls": 8, "seconds": 300}},
        },
        # Access (use), approval defaults per tier (tiers) and exceptions (rules); see ARCHITECTURE.md.
        "permissions": {
            "*": {"rules": {name: "auto" for name in BASIC_FEATURES}},
            "routine:*": {"use": ["basic.*"]},
        },
        # Files models may never read, besides the built-in list (.env, keys, credentials, modules' config.yml).
        "secrets": [],
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

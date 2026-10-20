"""Room extension: the shared multi-character room engine and its five default characters."""

from __future__ import annotations

from contracts import Contributions, Extension, Setting

from .engine import RoomEngine, flag, number
from .personas import PERSONAS

# What the room owns: each character's model profile, and Grok's provider (xAI, an OpenAI-compatible API; the
# others' providers are built into the core). The room's config.yml `providers:` and `profiles:` override
# these, and core.yml overrides both.
PROVIDERS = {"grok": {"kind": "openai_compatible", "base_url": "https://api.x.ai/v1", "api_key_env": "GROK_API_KEY",
                      "schema_mode": "native", "effort_param": True,
                      # $model offers her recent language models (not grok-imagine's pictures and videos).
                      "models_since": "2026-06-01", "models_exclude": ["imagine"]}}
OWN_MODELS = {
    "deepseek": [{"provider": "deepseek", "model": "deepseek-flash"}],
    "luna": [{"provider": "openai", "model": "gpt-6-luna"}],
    "gemini": [{"provider": "gemini", "model": "gemini-3.8-flash"}, {"provider": "gemini", "model": "gemini-3.6-flash"}],
    "claude": [{"provider": "claude", "model": "claude-haiku-5-5"}],
}
# Each character prefers her own model, then falls back to the others'; candidates without a key are skipped.
# Grok falls back to Luna's model first, then to the others' like everyone (so one key is always enough).
FALLBACK = [models[0] for models in OWN_MODELS.values()]
PROFILES = {name: [*own, *(item for item in FALLBACK if item not in own)] for name, own in OWN_MODELS.items()}
PROFILES["grok"] = [{"provider": "grok", "model": "grok-4-fast"}, *PROFILES["luna"]]


def setup(context) -> Contributions:
    config = context.config
    engine = RoomEngine(max_steps=number(config.get("max_steps"), 24, 4, 50),
                        history_limit=number(config.get("history_limit"), 30, 1, 150),
                        max_tokens=number(config.get("max_tokens"), 1500, 100, 32000),
                        aliases=config.get("aliases") or {"claude": ["Haiku"]},
                        chime_in=flag(config.get("chime_in"), False), compact=flag(config.get("compact"), True),
                        compact_every=number(config.get("compact_every"), 20, 1, 1000),
                        compact_profile=str(config.get("compact_profile") or "default"))
    providers = {name: dict(settings) for name, settings in PROVIDERS.items()}
    for name, settings in (config.get("providers") or {}).items():
        providers[name] = {**providers.get(name, {}), **dict(settings)}
    profiles = {**PROFILES, **dict(config.get("profiles") or {})}
    return Contributions(engines=[engine], personas=PERSONAS, providers=providers, profiles=profiles)


EXTENSION = Extension("room", setup, contracts="1", version="1",
                      description="Shared room: several personas work one request together", settings=(
    Setting("max_steps", "Most work-board steps per request", "number", 24),
    Setting("history_limit", "Earlier messages every character reads word for word", "number", 30),
    Setting("chime_in", "A group message naming no one: whose turn it is answers, the others may join in", "boolean",
            False),
    Setting("compact", "Fold messages older than history_limit into the chat's rolling summary", "boolean", True),
    Setting("grok_api_key", "xAI API key for Grok (without it, Luna's model answers for her)", "secret",
            env="GROK_API_KEY"),
))

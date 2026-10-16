"""Scholar: an example drop-in extension. A second Discord bot plus a slower, higher-effort engine.

Drop this folder into `plugins/` (it already is), copy `config.example.yml` to `config.yml` next to it,
put the bot token in `.env` and restart: the core loads it without changes to core.yml. Without a token
the engine and persona still load, so `deliberate` can be assigned to any conversation.

It contributes: the `scholar` interface (the Discord bot), the `deliberate` engine, the `scholar`
persona, the `scholar` model profile, and defaults for its own conversations (`scholar:*` uses the
deliberate engine; in servers it answers mentions and replies). core.yml settings win over all of these.
"""

from __future__ import annotations

import os

from contracts import Contributions, Extension, Persona

from .bot import ScholarBot
from .engine import DeliberateEngine

PROMPT = ("You are Scholar, a careful study companion. You think problems through, check facts with tools when "
          "they matter, show the reasoning that helps someone learn, and say plainly when you are unsure.")
MODELS = [
    {"provider": "claude", "model": "claude-opus-5-5"},
    {"provider": "openai", "model": "gpt-6.1-sol"},
    {"provider": "deepseek", "model": "deepseek-v4-pro"},
    {"provider": "gemini", "model": "gemini-3.8-flash"},
]


def setup(context) -> Contributions:
    config = context.config
    name = str(config.get("interface", "scholar"))
    persona = Persona("scholar", str(config.get("display_name", "Scholar")), str(config.get("prompt", PROMPT)),
                      profile="scholar")
    engine = DeliberateEngine(max_steps=int(config.get("max_steps", 12)), history_limit=int(config.get("history_limit", 24)),
                              effort=str(config.get("effort", "high")), max_tokens=int(config.get("max_tokens", 4000)),
                              review=bool(config.get("review", True)))
    contributions = {"engines": [engine], "personas": [persona],
                     "profiles": {"scholar": list(config.get("models") or MODELS)}}
    token = str(config.get("token") or os.getenv(str(config.get("token_env", "SCHOLAR_DISCORD_TOKEN")), "")).strip()
    if not token:
        context.log("no_token", hint="engine and persona only; set the bot token to start the Discord bot")
        return Contributions(**contributions)
    assignment = {"engine": "deliberate", "personas": ["scholar"],
                  "budget": {"model_calls": 40, "tool_calls": 24, "seconds": 1800},
                  **dict(config.get("assignment") or {})}
    return Contributions(**contributions, interfaces=[ScholarBot(name, token, context.data_dir)],
                         assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": True}})


EXTENSION = Extension("scholar", setup, contracts="1", version="1",
                      description="Example drop-in: a second Discord bot with a plan-work-review engine")

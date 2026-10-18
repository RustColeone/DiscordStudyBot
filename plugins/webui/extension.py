"""Web UI: the bot as a chat page on your local network, with no Discord needed.

Open http://<this computer>:4156 from any device at home. Whoever opens it is the bot's creator, so it
answers only the local network, refuses other websites and DNS-rebinding names, and can ask for a
passphrase once per browser (`passphrase`, or WEBUI_PASSPHRASE in .env). Conversations default to the
shared room: Luna, Gemini, Claude and DeepSeek working together, each in their own bubble with their own
picture (`avatars/<name>.png`). Markdown and LaTeX are shown as such; files go both ways.

It contributes the `webui` interface and defaults for its own conversations (`webui:*` uses the room
engine with the four classmates); core.yml settings win over these.
"""

from __future__ import annotations

import os
from pathlib import Path

from contracts import Contributions, Extension, Setting

from .interface import WebUI

CLASSMATES = ["luna", "gemini", "claude", "deepseek"]


def setup(context) -> Contributions:
    config = dict(context.config)
    name = str(config.get("interface", "webui"))
    config["passphrase"] = str(config.get("passphrase") or os.getenv(str(config.get("passphrase_env",
                                                                                     "WEBUI_PASSPHRASE")), ""))
    interface = WebUI(name, config, context.data_dir, Path(__file__).resolve().parent)
    assignment = {"engine": "room", "personas": list(config.get("personas") or CLASSMATES),
                  "budget": {"model_calls": 40, "tool_calls": 24, "seconds": 900},
                  **dict(config.get("assignment") or {})}
    return Contributions(interfaces=[interface], assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": False}})


EXTENSION = Extension("webui", setup, contracts="1", version="1",
                      description="A chat page on the local network: the four classmates, no Discord needed",
                      settings=(
    Setting("port", "Port the page is served on", "number", 4156),
    Setting("user_name", "Your name in the chat", "text"),
    Setting("title", "The page's title", "text", "Study Room"),
    Setting("passphrase", "Passphrase asked once per browser (empty: anyone on the local network)", "secret",
            env="WEBUI_PASSPHRASE"),
))

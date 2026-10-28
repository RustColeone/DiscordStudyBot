"""Web UI: the bot as a chat page on your local network, with no Discord needed.

Open http://<this computer>:4156 from any device at home. Whoever opens it is the bot's creator, so it
answers only the local network, refuses other websites and DNS-rebinding names, and can ask for a
passphrase once per browser (`passphrase`, or WEBUI_PASSPHRASE in .env). It is MomoChat's page: conversations
default to a group chat with GPT, Gemini, Claude, DeepSeek and Grok, each in their own bubble with their own
picture (from the students module, or `avatars/<name>.png`), and where each stands with you (her memory's alias,
bond and mood). Markdown and LaTeX are shown as such; files go both ways.

It contributes the `webui` interface and defaults for its own conversations (`webui:*` uses the momochat engine
with the five students); core.yml settings win over these. Without MomoChat (or the students) the page still
opens, greyed: nobody answers there, and it says so.
"""

from __future__ import annotations

import os
from pathlib import Path

from contracts import Contributions, Extension, Setting

from .interface import WebUI

# Who is in a chat by default; their names and pictures come from the core (the students module).
CLASSMATES = ["gpt", "gemini", "claude", "deepseek", "grok"]
# How MomoChat behaves here (the assignment's `options`, merged under `assignment: {options: ...}`): a long window
# with a rolling summary, and classmates who chime in. What a student remembers of you, in every chat, is her
# memory (the students module's).
OPTIONS = {"history_limit": 40, "compact": True, "chime_in": True}


def setup(context) -> Contributions:
    config = dict(context.config)
    name = str(config.get("interface", "webui"))
    config["passphrase"] = str(config.get("passphrase") or os.getenv(str(config.get("passphrase_env",
                                                                                     "WEBUI_PASSPHRASE")), ""))
    config["personas"] = [str(name) for name in config.get("personas") or CLASSMATES]
    interface = WebUI(name, config, context.data_dir, Path(__file__).resolve().parent)
    custom = dict(config.get("assignment") or {})
    assignment = {"engine": "momochat", "personas": list(config["personas"]),
                  "budget": {"model_calls": 40, "tool_calls": 24, "seconds": 900},
                  **custom, "options": {**OPTIONS, **dict(custom.get("options") or {})}}
    # Its chats need MomoChat and the students; without either the page still opens, greyed, and says so.
    # (`present` is None when set up outside a core.)
    present = getattr(context, "present", None)
    usable = present is None or {"momochat", "students"} <= set(present)
    interface.momochat = usable
    return Contributions(interfaces=[interface], assignments={f"{name}:*": assignment} if usable else {},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": False}})


EXTENSION = Extension("webui", setup, contracts="1", version="2", uses=("momochat", "students"),
                      description="MomoChat's page on the local network: the five students, no Discord needed",
                      settings=(
    Setting("port", "Port the page is served on", "number", 4156),
    Setting("user_name", "Your name in the chat", "text"),
    Setting("title", "The page's title", "text", "Study Room"),
    Setting("passphrase", "Passphrase asked once per browser (empty: anyone on the local network)", "secret",
            env="WEBUI_PASSPHRASE"),
))

"""Whale-chan (鲸酱): an example drop-in extension. Her own Discord bot, chatting like a personal AI web client
(ChatGPT, Claude) inside the app: a frontend of MomoChat. She is the DeepSeek student: the bot is one more way to
reach her, as MomoChat's other pages are, and what she is (her prompt, name, models) and remembers (her memory) is
the same everywhere.

Drop this folder into `plugins/` (it already is), put the bot token in `.env` as
DISCORD_BOT_TOKEN_WHALE_CHAN (or in this folder's config.yml) and restart: the core loads it without
changes to core.yml. Without a token only her voice loads.

It contributes: the `whale_chan` interface (the Discord bot, which also speaks her short replies as voice messages
where `/voice on` asks it to, and her own first messages), her voice (`whale_chan.speak`, for her model to call) and
defaults for its own conversations (`whale_chan:*` is MomoChat with the DeepSeek student, as a chat client: complete
answers, one thread until `/new`; in servers it answers mentions and replies; every write and critical action asks
the owner first, with no exceptions, in case she misread a request or a page tried to trick her). core.yml settings
win over all of these. Until 2026-10-27 it also had an engine of its own, `chat`: that is MomoChat's former name.
"""

from __future__ import annotations

import os

from contracts import Contributions, Extension, Setting

from . import voice
from .bot import WhaleChanBot


async def check_token(token: str) -> str:
    """Log in with the token over HTTP and name the bot."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("https://discord.com/api/v10/users/@me",
                                   headers={"Authorization": f"Bot {token.strip()}"}) as response:
                if response.status != 200:
                    raise ValueError("Discord rejected this token" if response.status == 401
                                     else f"Discord answered HTTP {response.status}")
                return f"logs in as {(await response.json()).get('username')}"
    except aiohttp.ClientError as error:
        raise ValueError(f"Could not reach Discord: {type(error).__name__}")

# She is the DeepSeek student (2026-10-27): her prompt, name and models are the students module's
# (extensions/students/deepseek/), and her memory and mood the companion's, as every student's. These settings of hers
# are read there now.
MOVED = {"prompt": "extensions/students/deepseek/ (whale.md, prompt.md)",
         "models": "extensions/students/deepseek/student.yml (models)",
         "display_name": "extensions/students/deepseek/student.yml (display_name, aliases)",
         "call_owner": "extensions/students/deepseek/student.yml (memory: call_owner, 主人 already)",
         "memory": "extensions/students/deepseek/student.yml (memory: language zh, memory_language Chinese already)",
         "mood": "extensions/students/config.yml (mood)",
         "diary": "nowhere: a student's memory is the students module's"}
# How MomoChat runs her chats: a chat client's, as her own engine did. Her settings of these names (effort,
# max_steps, ...) win, and the assignment's own `options` over them.
OPTIONS = {"style": "full", "effort": "high", "max_steps": 10, "max_tokens": 8000, "history_limit": 60,
           "history_chars": 40000, "timeout_s": 240, "compact": False, "new_chat_text": "🐳 好的主人，新的对话开始啦～"}


def chat_options(config) -> dict:
    """MomoChat's options for her chats: OPTIONS, with her settings of the same names (and `tool_calls`)."""
    return {**OPTIONS, **{key: config[key] for key in (*OPTIONS, "tool_calls") if key in config}}


def flag(value, default: bool = False) -> bool:
    """An on/off setting as people write it (true/false, yes/no, on/off, 1/0); else the default."""
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return True if text in ("true", "yes", "on", "1") else False if text in ("false", "no", "off", "0") else default


def setup(context) -> Contributions:
    config = context.config
    name = str(config.get("interface", "whale_chan"))
    persona = str(config.get("persona", "deepseek"))  # the student her bot speaks as
    moved = {key: where for key, where in MOVED.items() if key in config}
    if moved:
        context.log("settings_moved", keys=sorted(moved), to=sorted(set(moved.values())))
    speech = dict(config.get("voice") or {})
    speaking = speech.get("enabled", True) is not False
    capabilities = [voice.capability(speech)] if speaking else []
    token = str(config.get("token") or os.getenv(str(config.get("token_env", "DISCORD_BOT_TOKEN_WHALE_CHAN")), ""))
    token = token.strip().strip("\"'")
    if not token:
        context.log("no_token", hint="her voice only; set the bot token to start the Discord bot")
        return Contributions(capabilities=capabilities)
    custom = dict(config.get("assignment") or {})
    assignment = {"engine": "momochat", "personas": [persona],
                  "budget": {"model_calls": 30, "tool_calls": 24, "seconds": 1800},
                  **custom, "options": {**chat_options(config), **dict(custom.get("options") or {})}}
    bot = WhaleChanBot(name, token, context.data_dir, config=config, persona=persona,
                       voice={**speech, "enabled": speaking})
    return Contributions(capabilities=capabilities, interfaces=[bot],
                         assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": True}},
                         permissions={f"{name}:*": {"tiers": {"write": "always-ask", "critical": "always-ask"}}})


EXTENSION = Extension("whale_chan", setup, contracts="1", version="3", requires=("students", "momochat"),
                      description="Example drop-in: 鲸酱's own Discord bot, a personal AI chat client on MomoChat, "
                                  "speaking as the DeepSeek student", settings=(
    Setting("token", "鲸酱's own Discord bot token (a second application in the Developer Portal)", "secret",
            env="DISCORD_BOT_TOKEN_WHALE_CHAN", check=check_token),
    Setting("persona", "The student her bot speaks as", "text", "deepseek"),
))

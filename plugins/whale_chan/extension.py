"""Whale-chan (鲸酱): an example drop-in extension. Her own Discord bot, chatting like a personal AI web client
(ChatGPT, Claude) inside the app. She is the DeepSeek student: the bot is one more way to reach her, as MomoChat and
the room are, and what she is (her prompt, name, models) and remembers (the companion) is the same everywhere.

Drop this folder into `plugins/` (it already is), put the bot token in `.env` as
DISCORD_BOT_TOKEN_WHALE_CHAN (or in this folder's config.yml) and restart: the core loads it without
changes to core.yml. Without a token the `chat` engine still loads, so it can be assigned to any conversation.

It contributes: the `whale_chan` interface (the Discord bot), the `chat` engine, her voice (`whale_chan.speak`) and
defaults for its own conversations (`whale_chan:*` uses the chat engine with the DeepSeek student; in servers it
answers mentions and replies; every write and critical action asks the owner first, with no exceptions, in case she
misread a request or a page tried to trick her). core.yml settings win over all of these.
"""

from __future__ import annotations

import os

from contracts import Contributions, Extension, Setting

from . import voice
from .bot import WhaleChanBot
from .engine import ChatEngine


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
         "call_owner": "extensions/companion/config.yml (call_owner)",
         "memory": "extensions/companion/config.yml (memory), or `diary: true` here for the core diary",
         "mood": "extensions/companion/config.yml (mood)"}


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
    engine = ChatEngine(max_steps=int(config.get("max_steps", 10)), history_limit=int(config.get("history_limit", 60)),
                        history_chars=int(config.get("history_chars", 40000)), effort=str(config.get("effort", "high")),
                        max_tokens=int(config.get("max_tokens", 8000)),
                        new_chat_text=str(config.get("new_chat_text", "🐳 好的主人，新的对话开始啦～")),
                        diary=flag(config.get("diary")), tool_calls=config.get("tool_calls", True) is not False,
                        voice={"speak_up": speech.get("speak_up", True), "max_chars": int(speech.get("max_chars", 400))}
                        if speaking else None)
    contributions = {"engines": [engine], "capabilities": [voice.capability(speech)] if speaking else []}
    token = str(config.get("token") or os.getenv(str(config.get("token_env", "DISCORD_BOT_TOKEN_WHALE_CHAN")), ""))
    token = token.strip().strip("\"'")
    if not token:
        context.log("no_token", hint="the chat engine only; set the bot token to start the Discord bot")
        return Contributions(**contributions)
    assignment = {"engine": "chat", "personas": [persona],
                  "budget": {"model_calls": 30, "tool_calls": 24, "seconds": 1800},
                  **dict(config.get("assignment") or {})}
    bot = WhaleChanBot(name, token, context.data_dir, config=config, persona=persona)
    return Contributions(**contributions, interfaces=[bot],
                         assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": True}},
                         permissions={f"{name}:*": {"tiers": {"write": "always-ask", "critical": "always-ask"}}})


EXTENSION = Extension("whale_chan", setup, contracts="1", version="2", requires=("students",),
                      description="Example drop-in: 鲸酱's own Discord bot, a personal AI chat client, speaking as the "
                                  "DeepSeek student", settings=(
    Setting("token", "鲸酱's own Discord bot token (a second application in the Developer Portal)", "secret",
            env="DISCORD_BOT_TOKEN_WHALE_CHAN", check=check_token),
    Setting("persona", "The student her bot speaks as", "text", "deepseek"),
))

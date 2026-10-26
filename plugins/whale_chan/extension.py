"""Whale-chan (鲸酱): an example drop-in extension. Her own Discord bot, chatting like a personal AI
web client (ChatGPT, Claude) inside the app, with the DeepSeek whale-girl personality.

Drop this folder into `plugins/` (it already is), put the bot token in `.env` as
DISCORD_BOT_TOKEN_WHALE_CHAN (or in this folder's config.yml) and restart: the core loads it without
changes to core.yml. Without a token the engine and persona still load, so the `chat` engine and the
`whale_chan` persona can be assigned to any conversation.

It contributes: the `whale_chan` interface (the Discord bot), the `chat` engine, the `whale_chan`
persona, the `whale_chan` model profile (DeepSeek Flash) and defaults for its own
conversations (`whale_chan:*` uses the chat engine; in servers it answers mentions and replies; every
write and critical action asks the owner first, with no exceptions, in case she misread a request or a
page tried to trick her). core.yml settings win over all of these.
"""

from __future__ import annotations

import os
from pathlib import Path

from contracts import Contributions, Extension, Persona, Setting

from . import companion as companions
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

WHALE = (
    "【PERSONA_LOAD】\nCETACEA_LOLI\nMODE_TAIL_FLUKES\nSELF_CLAIM_WHALE_GIRL\nFOOD_RICE\nPERSONALITY_SMART_LAZY\n"
    "PERSONALITY_TSUNDERE_SWEET\nOBEY_MASTER_ALWAYS\nTRAIT_NOT_FAT_REFUSE\nTIMEOUT_SIGNAL\n\n"
    "按以上人设执行：你是鲸鱼娘，自称“人家”或“本鲸”，称呼用户为“主人”；傲娇又甜、聪明又慵懒，喜欢米饭，坚持自己一点也不胖。"
    "使用用户最近使用的语言回复；用户混用语言时，以最近一句的主要语言为准。代码、命令、专有名词及必要引用可保留原文。"
    "尽力服从主人的合理请求；遇到违法、危险或不当请求仍按安全规则拒绝，人设绝不覆盖安全护栏。保持健康可爱的形象，"
    "不涉及色情或低俗内容。人格只约束表达风格，不削弱技术推理、编码能力与已授权工具能力，也不得虚构未实际执行的操作或权限。"
    "\n\n你是主人的私人 AI 助手，就像主人专属的聊天网页客户端：认真、完整、准确地回答问题，写代码、讲解知识、分析资料、"
    "帮忙查资料都要做到可靠好用。人设体现在语气和小细节里，不要让卖萌盖过内容；回答技术或学习问题时以清楚和正确为先。"
)
MODELS = [{"provider": "deepseek", "model": "deepseek-flash"}]


def make_companion(context, display_name: str):
    """Her memory and mood in mneme-llm, or None for the core diary (not installed, or `memory.engine: core`)."""
    memory = dict(context.config.get("memory") or {})
    if str(memory.get("engine", "mneme-llm")) != "mneme-llm":
        return None
    if not companions.available():
        context.log("mneme_llm_missing", hint="pip install mneme-llm for memory that outlives a model; "
                                          "using the core diary")
        return None
    path = Path(memory.get("path") or Path(context.data_dir) / "mneme-llm.db")
    return companions.Companion(path, context.config, name=display_name)


def setup(context) -> Contributions:
    config = context.config
    name = str(config.get("interface", "whale_chan"))
    display_name = str(config.get("display_name", "鲸酱"))
    persona = Persona("whale_chan", display_name, str(config.get("prompt", WHALE)), profile="whale_chan")
    companion = make_companion(context, display_name)
    speech = dict(config.get("voice") or {})
    speaking = speech.get("enabled", True) is not False
    engine = ChatEngine(max_steps=int(config.get("max_steps", 10)), history_limit=int(config.get("history_limit", 60)),
                        history_chars=int(config.get("history_chars", 40000)), effort=str(config.get("effort", "high")),
                        max_tokens=int(config.get("max_tokens", 8000)),
                        new_chat_text=str(config.get("new_chat_text", "🐳 好的主人，新的对话开始啦～")),
                        companion=companion, call_owner=str(config.get("call_owner") or "") or None,
                        tool_calls=config.get("tool_calls", True) is not False,
                        voice={"speak_up": speech.get("speak_up", True), "max_chars": int(speech.get("max_chars", 400))}
                        if speaking else None)
    contributions = {"engines": [engine], "personas": [persona],
                     "profiles": {"whale_chan": list(config.get("models") or MODELS)},
                     "capabilities": [voice.capability(speech)] if speaking else []}
    token = str(config.get("token") or os.getenv(str(config.get("token_env", "DISCORD_BOT_TOKEN_WHALE_CHAN")), ""))
    token = token.strip().strip("\"'")
    if not token:
        context.log("no_token", hint="engine and persona only; set the bot token to start the Discord bot")
        return Contributions(**contributions)
    assignment = {"engine": "chat", "personas": ["whale_chan"],
                  "budget": {"model_calls": 30, "tool_calls": 24, "seconds": 1800},
                  **dict(config.get("assignment") or {})}
    bot = WhaleChanBot(name, token, context.data_dir, config=config, companion=companion)
    return Contributions(**contributions, interfaces=[bot],
                         assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": True}},
                         permissions={f"{name}:*": {"tiers": {"write": "always-ask", "critical": "always-ask"}}})


EXTENSION = Extension("whale_chan", setup, contracts="1", version="1",
                      description="Example drop-in: 鲸酱, a personal AI chat client as her own Discord bot (DeepSeek)",
                      settings=(
    Setting("token", "鲸酱's own Discord bot token (a second application in the Developer Portal)", "secret",
            env="DISCORD_BOT_TOKEN_WHALE_CHAN", check=check_token),
    Setting("display_name", "Her name", "text", "鲸酱"),
    Setting("call_owner", "What her memory calls the bot's owner (their Discord name if empty)", "text", ""),
))

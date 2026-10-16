"""Whale-chan (鲸酱): an example drop-in extension. Her own Discord bot, chatting like a personal AI
web client (ChatGPT, Claude) inside the app, with the DeepSeek whale-girl personality.

Drop this folder into `plugins/` (it already is), put the bot token in `.env` as
DISCORD_BOT_TOKEN_WHALE_CHAN (or in this folder's config.yml) and restart: the core loads it without
changes to core.yml. Without a token the engine and persona still load, so the `chat` engine and the
`whale_chan` persona can be assigned to any conversation.

It contributes: the `whale_chan` interface (the Discord bot), the `chat` engine, the `whale_chan`
persona, the `whale_chan` model profile (DeepSeek V4 Pro; DeepSeek Flash for images and as fallback) and
defaults for its own
conversations (`whale_chan:*` uses the chat engine; in servers it answers mentions and replies).
core.yml settings win over all of these.
"""

from __future__ import annotations

import os

from contracts import Contributions, Extension, Persona

from .bot import WhaleChanBot
from .engine import ChatEngine

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
# V4 Pro answers text; it cannot see images, so requests with images go to Flash first.
MODELS = [
    {"provider": "deepseek", "model": "deepseek-v4-pro", "vision": False},
    {"provider": "deepseek", "model": "deepseek-flash"},
]


def setup(context) -> Contributions:
    config = context.config
    name = str(config.get("interface", "whale_chan"))
    persona = Persona("whale_chan", str(config.get("display_name", "鲸酱")), str(config.get("prompt", WHALE)),
                      profile="whale_chan")
    engine = ChatEngine(max_steps=int(config.get("max_steps", 10)), history_limit=int(config.get("history_limit", 60)),
                        history_chars=int(config.get("history_chars", 40000)), effort=str(config.get("effort", "high")),
                        max_tokens=int(config.get("max_tokens", 8000)),
                        new_chat_text=str(config.get("new_chat_text", "🐳 好的主人，新的对话开始啦～")))
    contributions = {"engines": [engine], "personas": [persona],
                     "profiles": {"whale_chan": list(config.get("models") or MODELS)}}
    token = str(config.get("token") or os.getenv(str(config.get("token_env", "DISCORD_BOT_TOKEN_WHALE_CHAN")), ""))
    token = token.strip().strip("\"'")
    if not token:
        context.log("no_token", hint="engine and persona only; set the bot token to start the Discord bot")
        return Contributions(**contributions)
    assignment = {"engine": "chat", "personas": ["whale_chan"],
                  "budget": {"model_calls": 30, "tool_calls": 24, "seconds": 1800},
                  **dict(config.get("assignment") or {})}
    return Contributions(**contributions, interfaces=[WhaleChanBot(name, token, context.data_dir, config=config)],
                         assignments={f"{name}:*": assignment},
                         admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": True}})


EXTENSION = Extension("whale_chan", setup, contracts="1", version="1",
                      description="Example drop-in: 鲸酱, a personal AI chat client as her own Discord bot (DeepSeek)")

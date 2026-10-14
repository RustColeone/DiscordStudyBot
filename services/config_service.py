import json
import os
from typing import Dict

import yaml


ENV_OVERRIDE_KEYS = [
    "TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "SERP_API_KEY",
    "WOLFRAM_APPID",
    "WOLFRAM_PATH",
    "ID_CHANNEL",
    "ID_CHANNEL1",
    "ID_MESSAGE",
    "ID_VOICECHANNEL",
    "DISCORD_ALLOWED_CHANNELS",
    "DISCORD_REQUIRE_MENTION",
    "CREATOR_USER_ID",
    "CREATOR_ONLY_MODE",
    "CREATOR_PROMPT",
    "TIMEZONE",
    "AGENT_LOCALE",
    "AGENT_WORK_MAX_STEPS",
    "AGENT_WORK_MAX_ACTIONS",
    "MCP_SERVERS_JSON",
    "WECHAT_CHAT",
    "WECHAT_IDLE_CHAT",
    "WECHAT_BROADCAST_CHAT",
    "WECHAT_MONITOR_SECONDS",
    "WECHAT_WHITELIST",
]


def load_config(config_path: str = "config.yml") -> Dict:
    with open(config_path, "r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    for key in ENV_OVERRIDE_KEYS:
        env_value = os.getenv(key)
        if env_value:
            config[key] = env_value

    if config.get("MCP_SERVERS_JSON"):
        config["MCP_SERVERS"] = json.loads(config["MCP_SERVERS_JSON"])

    return config


def optional_secret(value) -> str:
    normalized = str(value or "").strip().strip("\"'")
    if normalized.lower() in {"", "disabled", "enabled", "none", "null"}:
        return ""
    return normalized

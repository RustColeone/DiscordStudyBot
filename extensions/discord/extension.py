"""Included Discord extension: connects the bot to Discord. Needs DISCORD_TOKEN (or TOKEN)."""

import os

from contracts import Contributions, Extension

from .interface import DiscordInterface


def setup(context) -> Contributions:
    config = context.config
    names = config.get("token_env") or ["DISCORD_TOKEN", "TOKEN"]
    names = [names] if isinstance(names, str) else list(names)
    token = next((os.environ[name].strip().strip("\"'") for name in names if os.environ.get(name, "").strip()), "")
    return Contributions(interfaces=[DiscordInterface(token, context.data_dir,
                                                      float(config.get("attachment_max_mb", 25)), config=config)])


EXTENSION = Extension("discord", setup, contracts="1", version="1",
                      description="Discord DMs and server channels (included)")

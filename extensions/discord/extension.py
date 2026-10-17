"""Included Discord extension: connects the bot to Discord. Needs DISCORD_BOT_TOKEN in .env
(DISCORD_TOKEN and TOKEN are still accepted)."""

import os

from contracts import Contributions, Extension, Setting

from .interface import DiscordInterface, INVITE_PERMISSIONS

API = "https://discord.com/api/v10"
MESSAGE_CONTENT = (1 << 18) | (1 << 19)  # application flags: the Message Content intent (full or limited)


async def check_token(token: str) -> str:
    """Log in with the token over HTTP: the bot's name, whether it can read messages, and its invite link."""
    import aiohttp
    import discord
    headers = {"Authorization": f"Bot {token.strip()}"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"{API}/users/@me", headers=headers) as response:
                if response.status == 401:
                    raise ValueError("Discord rejected this token (copy it again from Developer Portal → Bot)")
                if response.status != 200:
                    raise ValueError(f"Discord answered HTTP {response.status}")
                user = await response.json()
            async with session.get(f"{API}/applications/@me", headers=headers) as response:
                application = await response.json() if response.status == 200 else {}
    except aiohttp.ClientError as error:
        raise ValueError(f"Could not reach Discord: {type(error).__name__}")
    note = f"logs in as {user.get('username')}"
    if not int(application.get("flags") or 0) & MESSAGE_CONTENT:
        note += "; turn on Message Content Intent (Developer Portal → Bot → Privileged Gateway Intents)"
    if application.get("id"):
        note += "; invite it: " + discord.utils.oauth_url(int(application["id"]), permissions=INVITE_PERMISSIONS,
                                                           scopes=("bot",))
    return note


def setup(context) -> Contributions:
    config = context.config
    names = config.get("token_env") or ["DISCORD_BOT_TOKEN", "DISCORD_TOKEN", "TOKEN"]
    names = [names] if isinstance(names, str) else list(names)
    token = next((os.environ[name].strip().strip("\"'") for name in names if os.environ.get(name, "").strip()), "")
    return Contributions(interfaces=[DiscordInterface(token, context.data_dir,
                                                      float(config.get("attachment_max_mb", 25)), config=config)])


EXTENSION = Extension("discord", setup, contracts="1", version="1",
                      description="Discord DMs and server channels (included)", settings=(
    Setting("token", "The bot's token (Developer Portal → your application → Bot → Reset Token)", "secret",
            env=("DISCORD_BOT_TOKEN", "DISCORD_TOKEN", "TOKEN"), ask=True, check=check_token),
    Setting("thread_channels", "Channel IDs where a task asked to the bot gets its own thread", "list", []),
    Setting("talk_prefixes", "Words that address the bot like a mention, e.g. $agent", "list", []),
    Setting("slash_commands", "Offer the commands as Discord slash commands", "boolean", True),
    Setting("attachment_max_mb", "Largest attachment the bot downloads, in MB", "number", 25),
    Setting("math", "Render $$math$$ as images", "boolean", True),
    Setting("music_dir", "Folder of music for $music", "path"),
    Setting("music_list", "Text file listing YouTube music for $music", "path"),
))

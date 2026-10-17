"""Discord interface: DMs and server channels as conversations.

Conversations are `dm:<user id>` for direct messages and `<channel id>` for server channels and
threads. In servers the bot answers when mentioned or when someone replies to one of its messages
(the core's admission rules decide; the interface only reports the facts). A typed known `$command`
and every message in a thread the bot opened (`thread_channels`) or that was started on a bot message
count as addressed too.

Public operations act on the calling conversation only: music in the caller's voice channel of that
server (music.py), the study clock (clock.py). Text replies render math (math_render.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any

import discord

from contracts import (Address, Delivery, Display, FileRef, Interface, Operation, Reply, Request, Risk, Sender,
                       ToolResult)

from .clock import LiveClocks, clock_text
from .math_render import MathRenderer, fallback_text, split_math
from .music import CONTROL_ACTIONS, MusicCommand, MusicPlayer, is_url, parse_music_command
from . import slash
from .status_log import StatusLog, code_safe

DEFAULT_UPLOAD_BYTES = 10 * 1024 * 1024
MESSAGE_LIMIT = 2000
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                         create_public_threads=True, read_message_history=True, attach_files=True,
                                         embed_links=True, add_reactions=True)
NOTICES = {
    "run_failed": "⚠️ Sorry, I couldn't finish that.",
    "waiting_approval": "⏳ Waiting for the owner's approval (`{approval_id}`).",
    "unknown_command": "Unknown command `${command}`. Try `$help`.",
    "not_permitted": "That command isn't available here.",
    "locked": "🔧 The bot is in maintenance right now; please try again later.",
}


REFUSALS = {
    "disabled": "I don't answer in this channel.",
    "sender_not_allowed": "I can't take requests from you here.",
    "owner_only": "Only the owner can use me here.",
}
BUTTON_STYLES = {"primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary,
                 "success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger}
# A reaction on a message with buttons presses the first button of these styles.
REACTIONS = {"✅": ("success", "primary"), "👍": ("success", "primary"),
             "❌": ("danger",), "🛑": ("danger",), "👎": ("danger",)}
ACTION_MEMORY = 300
COMMAND = re.compile(r"^\$[A-Za-z]")


def reaction_action(emoji: str, actions) -> dict | None:
    styles = REACTIONS.get(emoji)
    if not styles or not actions:
        return None
    return next((action for action in actions if action.get("style") in styles), None)


def thread_name(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()[:80] or "Task"


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = 0
        for separator in ("\n\n", "\n", " "):
            boundary = remaining.rfind(separator, 0, limit - len(separator) + 1)
            if boundary >= limit // 2:
                cut = boundary + len(separator)
                break
        cut = cut or limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def inline_code(text: str) -> str:
    return "`" + str(text).replace("`", "ˋ") + "`"


def attachment_kind(content_type: str) -> str:
    content_type = (content_type or "").lower()
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type.startswith("audio/"):
        return "voice"
    return "file"


class DiscordInterface(Interface):
    name = "discord"
    display = Display(markdown=True, edits=True, progress=True, quotes=True, files=True,
                      max_upload_mb=DEFAULT_UPLOAD_BYTES / 1024 / 1024, max_text_chars=MESSAGE_LIMIT)
    operations = (
        Operation("status", "Discord connection status, servers and owners", command="discord_status"),
        Operation("invite", "The link for inviting this bot to a server", command="invite"),
        Operation("music", "The music player in the caller's voice channel in this server. Text: init, play, pause, "
                  "stop, next, prev, status, queue, or -y <YouTube URLs> [--queue] (play now, or add after the "
                  "current song).", {"command": {"type": "string"}}, risk=Risk.WRITE, command="music",
                  command_argument="command", timeout_s=180, owner_only=False),
        Operation("music_status", "Show this server's music player track and position.", owner_only=False),
        Operation("music_control", "Initialize (join the caller's voice channel and play), play, pause, stop, or "
                  "change tracks in this server's music player.",
                  {"action": {"type": "string", "enum": list(CONTROL_ACTIONS)}}, ("action",), Risk.WRITE,
                  timeout_s=180, owner_only=False),
        Operation("queue_music", "Add a YouTube music URL after the current song in this server's playlist; "
                  "play_now plays it immediately in the caller's voice channel. Resolve references such as "
                  "'that music' from the conversation and pass its URL.",
                  {"url": {"type": "string"}, "play_now": {"type": "boolean"}}, ("url",), Risk.WRITE,
                  timeout_s=180, owner_only=False),
        Operation("time", "The study clock: UTC minutes:seconds (break time from minute 45) and Beijing, "
                  "Los Angeles and London times.", command="time", owner_only=False),
        Operation("clock_start", "Start the live study clock (one message edited every second) in this "
                  "conversation.", risk=Risk.WRITE, command="start", owner_only=False),
        Operation("clock_stop", "Stop the live study clock in this conversation.", risk=Risk.WRITE, command="stop",
                  owner_only=False),
    )

    def __init__(self, token: str, data_dir: Path, attachment_max_mb: float = 25.0, client: Any = None,
                 config: Any = None):
        config = dict(config or {})
        self.token = token
        self.attachments_dir = Path(data_dir) / "attachments"
        self.attachment_max_bytes = int(attachment_max_mb * 1024 * 1024)
        # Legacy habits such as "$agent hi": the prefix is dropped and the message counts as addressed.
        self.talk_prefixes = tuple(str(item).strip() for item in config.get("talk_prefixes") or () if str(item).strip())
        # Channels where a message addressed to the bot opens a thread on it, and the task runs there.
        self.thread_channels = frozenset(str(item).strip() for item in config.get("thread_channels") or ())
        self.client = client
        self.port = None
        self._task: asyncio.Task | None = None
        self._channels: OrderedDict[str, Any] = OrderedDict()
        # Progress message per run, and the interaction whose placeholder shows it (None: a channel message).
        self._progress: OrderedDict[str, tuple[Any, str | None]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owners: list[str] = []
        self.status_log = StatusLog(self._send_status, self._edit_status)
        self._members: OrderedDict[tuple[str, str], Any] = OrderedDict()
        self._sent_ids: OrderedDict[str, None] = OrderedDict()
        self._thread_starters: OrderedDict[str, bool] = OrderedDict()
        self.math = MathRenderer(Path(data_dir) / "math", config)
        music_list = config.get("music_list")
        self.music = MusicPlayer(self, Path(config.get("music_dir") or Path(data_dir) / "music").expanduser(),
                                 Path(music_list).expanduser() if music_list else None,
                                 str(config.get("ffmpeg") or "ffmpeg"))
        self.clocks = LiveClocks(self._channel, float(config.get("clock_interval_s", 1.0)),
                                 int(config.get("clock_max", 10)))
        self.slash_commands = bool(config.get("slash_commands", True))
        self.interactions = slash.Interactions()

    # ---- lifecycle --------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        self._prune_attachments()
        if not self.token:
            print("[discord] No bot token: set DISCORD_BOT_TOKEN in .env. Discord is not connected.", flush=True)
            port.log("token_missing")
            port.set_ready(False, "No Discord bot token is configured.")
            return
        if self.client is None:
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_interaction)
        self.client.event(self.on_raw_reaction_add)
        self._task = asyncio.create_task(self._run(), name="discord-client")

    async def _run(self) -> None:
        try:
            await self.client.start(self.token)
        except discord.PrivilegedIntentsRequired:
            print("[discord] The Message Content intent is disabled. Enable it in the Discord Developer Portal "
                  "(Bot → Privileged Gateway Intents) and restart.", flush=True)
            self.port.set_ready(False, "Message Content intent is not enabled for this bot.")
        except discord.LoginFailure:
            print("[discord] The bot token was rejected. Check DISCORD_BOT_TOKEN.", flush=True)
            self.port.set_ready(False, "The Discord token was rejected.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[discord] Connection stopped: {type(error).__name__}: {error}", flush=True)
            self.port.log("client_stopped", error=type(error).__name__)

    async def stop(self) -> None:
        await self.clocks.stop_all()
        await self.music.close()
        if self.client is not None and not self.client.is_closed():
            await self.client.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _prune_attachments(self, keep_days: float = 7.0) -> None:
        if not self.attachments_dir.is_dir():
            return
        cutoff = time.time() - keep_days * 86400
        for directory in self.attachments_dir.iterdir():
            if directory.is_dir() and directory.stat().st_mtime < cutoff:
                for file in directory.iterdir():
                    file.unlink(missing_ok=True)
                directory.rmdir()

    # ---- events -----------------------------------------------------------------------------------
    async def on_ready(self) -> None:
        user = self.client.user
        try:
            application = await self.client.application_info()
            team = getattr(application, "team", None)
            owners = [str(member.id) for member in team.members] if team else [str(application.owner.id)]
        except Exception as error:
            owners = []
            self.port.log("owner_lookup_failed", error=type(error).__name__)
        if owners:
            self._owners = owners
            self.port.set_owners(owners, control=Address(self.name, f"dm:{owners[0]}"))
        self.port.set_ready(True, f"Logged in as {user}.")
        await self.sync_commands()
        guilds = len(getattr(self.client, "guilds", []) or [])
        message = f"[discord] Logged in as {user} in {guilds} server(s)."
        if not guilds:
            message += f" Invite it: {self.invite_url()}"
        print(message, flush=True)

    async def on_message(self, message) -> None:
        user = self.client.user
        author = message.author
        if getattr(author, "bot", False) or (user is not None and author.id == user.id):
            return
        conversation = self.conversation_id(message.channel, author)
        self._remember(conversation, message.channel)
        if message.guild is not None:
            self._members[(str(message.guild.id), str(author.id))] = author
            self._members.move_to_end((str(message.guild.id), str(author.id)))
            while len(self._members) > 512:
                self._members.popitem(last=False)
        async with self._locks[conversation]:
            request = await self.to_request(message, conversation)
            request = await self._open_thread(message, request)
            result = await self.port.submit(request)
        if result and result.reason == "accepted":
            try:
                await self._channels.get(request.address.conversation, message.channel).typing()
            except Exception:
                pass

    async def _open_thread(self, message, request: Request) -> Request:
        """In a thread channel, a message addressed to the bot (not a command) becomes a thread's first
        message: the run, its questions and follow-ups happen in the thread's own conversation."""
        if (message.guild is None or str(message.channel.id) not in self.thread_channels
                or request.mentions_me is not True or COMMAND.match(request.text)):
            return request
        try:
            thread = await message.create_thread(name=thread_name(request.text), auto_archive_duration=1440)
        except (discord.HTTPException, AttributeError) as error:
            self.port.log("thread_failed", error=type(error).__name__, detail=str(error)[:200])
            return request
        self._remember(str(thread.id), thread)
        return dataclasses.replace(request, address=Address(self.name, str(thread.id)),
                                   extra={**request.extra, "channel_name": getattr(thread, "name", "") or "",
                                          "parent_channel": str(message.channel.id)})

    async def on_raw_reaction_add(self, payload) -> None:
        """A reaction on a message with buttons presses the matching button (see REACTIONS)."""
        user = self.client.user
        if user is None or payload.user_id == user.id or getattr(payload.member, "bot", False):
            return
        actions = (self.port.bottle.get("message_actions") or {}).get(str(payload.message_id))
        action = reaction_action(str(payload.emoji), actions)
        if action is None:
            return
        member = payload.member
        if member is None:
            member = self.client.get_user(payload.user_id) or await self.client.fetch_user(payload.user_id)
        guild = getattr(payload.member, "guild", None)
        conversation = str(payload.channel_id) if payload.guild_id else f"dm:{payload.user_id}"
        request = Request(
            id=f"reaction:{payload.message_id}:{payload.user_id}:{action['command']}"[:200],
            address=Address(self.name, conversation),
            sender=Sender(str(payload.user_id), getattr(member, "display_name", None) or getattr(member, "name", "")),
            text=action["command"], received_at=time.time(), is_direct=not payload.guild_id, mentions_me=True,
            display=self.display_for_guild(guild),
            extra={"guild_id": str(payload.guild_id or ""), "via": "reaction"})
        async with self._locks[conversation]:
            await self.port.submit(request)

    def _remember_actions(self, message_id, reply: Reply) -> None:
        if not reply.actions:
            return
        stored = dict(self.port.bottle.get("message_actions") or {})
        stored[str(message_id)] = [{"command": action.command, "style": action.style} for action in reply.actions]
        while len(stored) > ACTION_MEMORY:
            stored.pop(next(iter(stored)))
        self.port.bottle.set("message_actions", stored)

    def _forget_actions(self, message_id) -> None:
        stored = self.port.bottle.get("message_actions") or {}
        if str(message_id) in stored:
            self.port.bottle.set("message_actions", {key: value for key, value in stored.items()
                                                     if key != str(message_id)})

    def conversation_id(self, channel, author) -> str:
        if getattr(channel, "type", None) == discord.ChannelType.private:
            return f"dm:{author.id}"
        return str(channel.id)

    async def is_bot_thread(self, channel) -> bool:
        """A thread the bot opened, or one started on a bot message (a thread's ID is its starter
        message's). Recent bot messages are known; otherwise the starter is looked up once per thread."""
        user = self.client.user
        if user is None or getattr(channel, "parent_id", None) is None:
            return False
        if getattr(channel, "owner_id", None) == user.id:
            return True
        key = str(channel.id)
        if key in self._sent_ids:
            return True
        if key not in self._thread_starters:
            starter = getattr(channel, "starter_message", None)
            if starter is None:
                try:
                    starter = await channel.parent.fetch_message(channel.id)
                except (discord.HTTPException, AttributeError):
                    starter = None
            self._thread_starters[key] = getattr(getattr(starter, "author", None), "id", None) == user.id
            while len(self._thread_starters) > 512:
                self._thread_starters.popitem(last=False)
        return self._thread_starters[key]

    def _sent(self, messages) -> None:
        for message in messages:
            self._sent_ids[str(message.id)] = None
        while len(self._sent_ids) > 2000:
            self._sent_ids.popitem(last=False)

    async def to_request(self, message, conversation: str) -> Request:
        user = self.client.user
        channel, guild = message.channel, message.guild
        is_direct = getattr(channel, "type", None) == discord.ChannelType.private
        content = message.content or ""
        mentioned = user is not None and any(getattr(member, "id", None) == user.id for member in message.mentions)
        reference = message.reference
        resolved = getattr(reference, "resolved", None) if reference else None
        resolved_message = resolved if hasattr(resolved, "author") else None
        replied_to_me = (resolved_message is not None and user is not None
                         and getattr(resolved_message.author, "id", None) == user.id)
        if user is not None:
            content = re.sub(rf"<@!?{user.id}>", "", content).strip()
        prefixed = next((prefix for prefix in self.talk_prefixes
                         if content == prefix or content.startswith(prefix + " ")), None)
        if prefixed:
            content = content[len(prefixed):].strip()
        # Typing a known command ("$help") addresses the bot as a slash command does; "$AAPL" does not.
        commanded = not prefixed and self.names_command(content)
        # A thread the bot opened, or one someone started on a bot message, is a conversation with the
        # bot: everything there is addressed.
        own_thread = await self.is_bot_thread(channel)
        addressed = is_direct or mentioned or replied_to_me or bool(prefixed) or commanded or own_thread
        files, kinds = [], []
        for attachment in message.attachments or []:
            kinds.append(attachment_kind(getattr(attachment, "content_type", "")))
            if addressed and attachment.size <= self.attachment_max_bytes:
                files.append(await self._download(message.id, attachment))
        kind = kinds[0] if kinds else "text"
        if not content and kinds:
            content = " ".join(f"[{item}]" for item in kinds)
        display = self.display_for_guild(guild)
        name = getattr(message.author, "display_name", None) or message.author.name
        return Request(
            id=str(message.id), address=Address(self.name, conversation), sender=Sender(str(message.author.id), name),
            text=content, received_at=message.created_at.timestamp(), is_direct=is_direct,
            mentions_me=True if is_direct else addressed,
            reply_to=str(reference.message_id) if reference and reference.message_id else None,
            quoted_text=(resolved_message.content or "")[:1000] if resolved_message is not None else "",
            kind=kind, files=tuple(files), display=display,
            extra={"guild_id": str(guild.id) if guild else "", "channel_name": getattr(channel, "name", "") or ""})

    def names_command(self, content: str) -> bool:
        if not content.startswith("$"):
            return False
        name = content[1:].partition(" ")[0].strip().lower()
        return bool(name) and any(command.name == name for command in self.port.commands())

    @staticmethod
    def display_for_guild(guild) -> Display:
        limit = getattr(guild, "filesize_limit", DEFAULT_UPLOAD_BYTES) if guild else DEFAULT_UPLOAD_BYTES
        return Display(markdown=True, edits=True, progress=True, quotes=True, files=True,
                       max_upload_mb=round(limit / 1024 / 1024, 2), max_text_chars=MESSAGE_LIMIT)

    # ---- slash commands, message actions and buttons -----------------------------------------------
    async def sync_commands(self) -> None:
        """Register the core's command catalog as application commands (only when it changed)."""
        http = getattr(self.client, "http", None)
        if not self.slash_commands or http is None:
            return
        definitions = slash.payload(self.port.commands())
        digest = slash.fingerprint(definitions)
        if self.port.bottle.get("slash_fingerprint") == digest:
            return
        try:
            await http.bulk_upsert_global_commands(self.client.application_id, definitions)
        except Exception as error:
            # Commands keep working as `$` messages; the next start tries again.
            self.port.log("slash_sync_failed", error=type(error).__name__, detail=str(error)[:200])
            print(f"[discord] Could not register slash commands: {type(error).__name__}: {error}"[:300], flush=True)
            return
        self.port.bottle.set("slash_fingerprint", digest)
        self.port.log("slash_synced", commands=len(definitions))

    def _interaction_conversation(self, interaction) -> str:
        if interaction.guild is None:
            return f"dm:{interaction.user.id}"
        return str(interaction.channel_id)

    async def on_interaction(self, interaction) -> None:
        try:
            if interaction.type == discord.InteractionType.application_command:
                await self._on_command(interaction)
            elif (interaction.type == discord.InteractionType.component
                  and str((interaction.data or {}).get("custom_id", "")).startswith(slash.BUTTON_PREFIX)):
                await self._on_button(interaction)
        except Exception as error:
            self.port.log("interaction_failed", error=type(error).__name__, detail=str(error)[:200])

    async def _on_command(self, interaction) -> None:
        data = interaction.data or {}
        command = slash.find(self.port.commands(), data)
        if command is None:
            await interaction.response.send_message("That command is no longer available.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        values, attachments = slash.values(command, data)
        files = [await self._download_raw(interaction.id, item) for item in attachments
                 if int(item.get("size") or 0) <= self.attachment_max_bytes]
        await self._submit_interaction(interaction, command.render(values), tuple(file for file in files if file),
                                       placeholder=True)

    async def _on_button(self, interaction) -> None:
        await interaction.response.defer()
        command = str(interaction.data["custom_id"])[len(slash.BUTTON_PREFIX):]
        await self._submit_interaction(interaction, command, (), placeholder=False)

    async def _submit_interaction(self, interaction, text: str, files: tuple, placeholder: bool) -> None:
        conversation = self._interaction_conversation(interaction)
        if interaction.channel is not None and hasattr(interaction.channel, "send"):
            self._remember(conversation, interaction.channel)
        pending = self.interactions.add(str(interaction.id), interaction, conversation, placeholder)
        user = interaction.user
        request = Request(
            id=str(interaction.id), address=Address(self.name, conversation),
            sender=Sender(str(user.id), getattr(user, "display_name", None) or user.name), text=text,
            received_at=interaction.created_at.timestamp(), is_direct=interaction.guild is None, mentions_me=True,
            files=files, display=self.display_for_guild(interaction.guild),
            extra={"guild_id": str(interaction.guild.id) if interaction.guild else "", "via": "interaction"})
        async with self._locks[conversation]:
            result = await self.port.submit(request)
        if not result and result.reason in REFUSALS and pending.placeholder:
            pending.placeholder = False
            await interaction.edit_original_response(content=REFUSALS[result.reason])

    async def _download_raw(self, interaction_id, attachment: dict) -> FileRef | None:
        directory = self.attachments_dir / str(interaction_id)
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\-]+", "_", str(attachment.get("filename") or "attachment"))[:120] or "attachment"
        try:
            data = await self.client.http.get_from_cdn(attachment["url"])
        except (discord.HTTPException, KeyError) as error:
            self.port.log("attachment_failed", error=type(error).__name__)
            return None
        path = directory / name
        path.write_bytes(data)
        return FileRef(str(path), str(attachment.get("filename") or name), str(attachment.get("content_type") or ""),
                       len(data))

    def _view(self, reply: Reply):
        if not reply.actions:
            return None
        view = discord.ui.View(timeout=None)
        for action in reply.actions[:5]:
            view.add_item(discord.ui.Button(label=action.label[:80], style=BUTTON_STYLES.get(action.style,
                                                                                            discord.ButtonStyle.secondary),
                                            custom_id=(slash.BUTTON_PREFIX + action.command)[:100]))
        return view

    def _poster(self, reply: Reply, channel):
        """How to post this reply: answering its interaction while that is possible, else to the channel."""
        pending = self.interactions.get(reply.in_reply_to)
        if pending is not None and pending.conversation != reply.address.conversation:
            pending = None

        async def post(content=None, file=None, reference=None, allowed_mentions=None, view=None):
            options = {key: value for key, value in (("content", content), ("file", file), ("view", view),
                                                     ("allowed_mentions", allowed_mentions)) if value is not None}
            if pending is None:
                return await channel.send(reference=reference, **options)
            interaction = pending.interaction
            if pending.placeholder:
                pending.placeholder = False
                if not reply.private:
                    edit = {key: value for key, value in options.items() if key != "file"}
                    edit["view"] = view  # None also removes buttons a progress update left on the placeholder
                    if file is not None:
                        edit["attachments"] = [file]
                    return await interaction.edit_original_response(**edit)
                try:
                    await interaction.delete_original_response()
                except discord.HTTPException:
                    pass
            return await interaction.followup.send(ephemeral=reply.private, wait=True, **options)
        return post, pending

    async def _download(self, message_id, attachment) -> FileRef:
        directory = self.attachments_dir / str(message_id)
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\-]+", "_", attachment.filename)[:120] or "attachment"
        path = directory / name
        await attachment.save(path)
        return FileRef(str(path), attachment.filename, getattr(attachment, "content_type", "") or "", attachment.size)

    # ---- delivery ---------------------------------------------------------------------------------
    def _remember(self, conversation: str, channel) -> None:
        self._channels[conversation] = channel
        self._channels.move_to_end(conversation)
        while len(self._channels) > 256:
            self._channels.popitem(last=False)

    async def _channel(self, conversation: str):
        if conversation in self._channels:
            return self._channels[conversation]
        try:
            if conversation.startswith("dm:"):
                user_id = int(conversation[3:])
                user = self.client.get_user(user_id) or await self.client.fetch_user(user_id)
                channel = user.dm_channel or await user.create_dm()
            else:
                channel_id = int(conversation)
                channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        except (ValueError, discord.NotFound, discord.Forbidden):
            return None
        self._remember(conversation, channel)
        return channel

    @staticmethod
    def is_status(reply: Reply) -> bool:
        """Information nobody explicitly asked for: progress of work in another conversation, failure
        details for the owner and relay confirmations. It goes into the rolling status block."""
        if reply.kind == "progress":
            return bool(reply.data.get("origin_address"))
        if reply.kind == "notice":
            return reply.code == "run_failed_detail" or (reply.code == "relayed" and reply.data.get("ok") == "true")
        if reply.kind == "event":
            # Interface status (no originating chat to reply to); chat notifications stay separate.
            return str(reply.data.get("origin_address", "")).endswith(":_interface") and reply.file is None
        return False

    def status_line(self, reply: Reply) -> str:
        origin = reply.data.get("origin_address")
        if reply.kind == "progress":
            return f"{origin} · {reply.text}"
        if reply.code == "relayed":
            return f"sent to {reply.data.get('address')}"
        if reply.kind == "event":
            return f"{reply.data.get('interface', '')} · {reply.text}"
        return reply.text

    def render_approval(self, reply: Reply) -> str:
        data = dict(reply.data)
        approval, arguments = data.get("approval_id", ""), data.get("arguments", "{}")
        head = f"🔐 Approval {inline_code(approval)} · {inline_code(data.get('capability', ''))}"
        if len(arguments) <= 300 and "\n" not in arguments:
            lines = [f"{head} {inline_code(arguments)}"]
        else:
            lines = [head, "```json\n" + code_safe(arguments[:1500]) + "\n```"]
        run = f" (run {inline_code(reply.run_id)})" if reply.run_id else ""
        lines.append(f"Requested in {inline_code(data.get('address', ''))}{run}. "
                     f"Reply {inline_code('$approve ' + approval)} or {inline_code('$deny ' + approval)}.")
        return "\n".join(lines)

    @staticmethod
    def render_reminder(reply: Reply) -> str:
        sender = reply.data.get("sender_id", "")
        text = f"⏰ {f'<@{sender}> ' if sender.isdigit() else ''}{reply.text}"
        try:
            due = float(reply.data.get("at", ""))
        except ValueError:
            return text
        return text + (f"\n-# due <t:{int(due)}:f>" if time.time() - due > 300 else "")

    @staticmethod
    def mentions_for(reply: Reply) -> discord.AllowedMentions | None:
        sender = reply.data.get("sender_id", "")
        if reply.kind == "notice" and reply.code == "reminder" and sender.isdigit():
            return discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(int(sender))])
        return None

    def render(self, reply: Reply) -> str:
        data = dict(reply.data)
        if reply.kind == "notice":
            if reply.code == "relayed":
                return (f"↪️ Sent to `{data.get('address')}`." if data.get("ok") == "true"
                        else f"⚠️ Not sent to `{data.get('address')}`: {data.get('detail') or 'unknown error'}")
            if reply.code == "approval_request" and data.get("approval_id"):
                return self.render_approval(reply)
            if reply.code == "run_failed_detail":
                return "⚠️ " + reply.text
            if reply.code == "reminder":
                return self.render_reminder(reply)
            if reply.code in NOTICES:
                return NOTICES[reply.code].format(**{"approval_id": "", "command": "", **data})
            return reply.text or f"[{reply.code}]"
        if reply.kind == "event":
            origin = data.get("origin_address", "")
            footer = f"\n-# {origin}" if origin and not origin.endswith(":_interface") else ""
            return f"📨 {reply.text}{footer}" if reply.text else ""
        origin = data.get("origin_address")
        speaker = f"**{reply.speaker}{' · ' + reply.label if reply.label else ''}**\n" if reply.speaker else ""
        return (f"-# from `{origin}`\n" if origin else "") + speaker + (reply.text or "")

    async def deliver(self, reply: Reply) -> Delivery:
        if self.client is None:
            return Delivery(False, detail="not_connected")
        channel = await self._channel(reply.address.conversation)
        if channel is None:
            return Delivery(False, detail="unknown_conversation")
        if self.is_status(reply):
            try:
                message = await self.status_log.append(reply.address.conversation, self.status_line(reply))
            except discord.HTTPException as error:
                return Delivery(False, detail=f"HTTP {getattr(error, 'status', '?')}")
            return Delivery(message is not None, message_id=str(message.id) if message is not None else None)
        post, pending = self._poster(reply, channel)
        if reply.kind == "progress":
            return await self._show_progress(channel, reply, pending)
        # The interaction whose placeholder this reply takes over (with any progress shown in it).
        taken_over = reply.in_reply_to if pending is not None and pending.placeholder else None
        reference = None
        if (pending is None and reply.quote and str(reply.quote).isdigit()
                and str(reply.quote) != reply.address.conversation):  # a thread's starter message is outside it
            reference = discord.MessageReference(message_id=int(reply.quote), channel_id=channel.id,
                                                 fail_if_not_exists=False)
        text = self.render(reply)
        sent, items, view = [], [], None
        try:
            if reply.kind == "file":
                file = discord.File(reply.file.path, filename=reply.file.name or Path(reply.file.path).name)
                sent.append(await post(content=text[:MESSAGE_LIMIT] or None, file=file, reference=reference))
            else:
                if not text.strip():
                    return Delivery(True, detail="suppressed")
                mentions = self.mentions_for(reply)
                view = self._view(reply)
                items = await self._text_items(reply, text)
                for index, (kind, value) in enumerate(items):
                    first, last = index == 0, index == len(items) - 1
                    if kind == "image":
                        sent.append(await post(file=discord.File(value, filename="formula.png"),
                                               reference=reference if first else None))
                    else:
                        sent.append(await post(content=value, reference=reference if first else None,
                                               allowed_mentions=mentions if first else None,
                                               view=view if last else None))
        except (discord.Forbidden, discord.NotFound) as error:
            return Delivery(False, detail=f"{type(error).__name__}: {error}"[:300], uncertain=bool(sent))
        except discord.HTTPException as error:
            certain = getattr(error, "status", 500) < 500 and not sent
            return Delivery(False, detail=f"HTTP {getattr(error, 'status', '?')}: {error}"[:300], uncertain=not certain)
        except (OSError, asyncio.TimeoutError) as error:
            return Delivery(False, detail=f"{type(error).__name__}: {error}"[:300], uncertain=True)
        finally:
            for kind, value in items:
                if kind == "image":
                    Path(value).unlink(missing_ok=True)
        self._sent(sent)
        await self._clear_progress(reply.run_id, taken_over)
        if view is not None and sent:
            self._remember_actions(sent[-1].id, reply)
        return Delivery(True, message_id=str(sent[0].id))

    async def _text_items(self, reply: Reply, text: str) -> list[tuple[str, Any]]:
        """Text chunks and rendered display-math images (`("text", str)` / `("image", path)`) in order."""
        if (reply.kind != "text" or reply.source == "owner" or not self.math.enabled
                or ("$" not in text and "\\" not in text)):
            return [("text", chunk) for chunk in split_text(text)]
        items, images = [], 0
        for kind, value in split_math(text):
            if kind == "text":
                items.extend(("text", chunk) for chunk in split_text(value) if value.strip())
                continue
            path = None
            if images < self.math.max_images:
                try:
                    path = await asyncio.to_thread(self.math.render, value)
                    images += 1
                except Exception as error:
                    self.port.log("math_render_failed", error=type(error).__name__)
            items.extend([("image", path)] if path else [("text", chunk) for chunk in split_text(fallback_text(value))])
        return items or [("text", chunk) for chunk in split_text(text)]

    async def _send_status(self, conversation: str, text: str):
        channel = await self._channel(conversation)
        return None if channel is None else await channel.send(content=text)

    @staticmethod
    async def _edit_status(message, text: str) -> None:
        await message.edit(content=text)

    async def _show_progress(self, channel, reply: Reply, pending=None) -> Delivery:
        """One progress message per run, edited in place, with the run's Cancel button. A slash command's
        "thinking…" placeholder shows the progress itself until the answer replaces it."""
        key = reply.run_id or reply.in_reply_to or reply.address.conversation
        text = ("⏳ " + self.render(reply))[:MESSAGE_LIMIT]
        view = self._view(reply)
        previous = self._progress.get(key)
        entry = None
        try:
            if pending is not None and pending.placeholder:
                entry = (await pending.interaction.edit_original_response(content=text, view=view), reply.in_reply_to)
            elif previous is not None:
                message = self._editable(previous)
                try:
                    await message.edit(content=text, view=view)
                    entry = (message, previous[1] if message is previous[0] else None)
                except discord.HTTPException:
                    entry = None  # gone or no longer editable: post a fresh one
            if entry is None:
                entry = (await channel.send(content=text, view=view), None)
        except discord.HTTPException as error:
            return Delivery(False, detail=f"HTTP {getattr(error, 'status', '?')}")
        message = entry[0]
        if previous is None or str(previous[0].id) != str(message.id):
            if previous is not None:
                self._forget_actions(previous[0].id)
            if view is not None:
                self._remember_actions(message.id, reply)
        self._progress[key] = entry
        while len(self._progress) > 200:
            self._progress.popitem(last=False)
        return Delivery(True, message_id=str(message.id))

    def _editable(self, entry):
        """The progress message, edited through its interaction only while that can still be answered;
        after that, through the channel with the bot's own token."""
        message, interaction_id = entry
        if interaction_id is None or self.interactions.get(interaction_id) is not None:
            return message
        partial = getattr(getattr(message, "channel", None), "get_partial_message", None)
        return partial(message.id) if partial is not None else message

    async def _clear_progress(self, run_id: str | None, taken_over: str | None = None) -> None:
        """Remove a run's progress message, unless the answer `taken_over` its placeholder now fills it."""
        entry = self._progress.pop(run_id, None) if run_id else None
        if entry is None:
            return
        self._forget_actions(entry[0].id)
        if entry[1] is not None and entry[1] == taken_over:
            return
        try:
            await self._editable(entry).delete()
        except discord.HTTPException:
            pass

    # ---- operations -------------------------------------------------------------------------------
    def invite_url(self) -> str:
        user = self.client.user if self.client is not None else None
        if user is None:
            return "(available after the bot logs in)"
        return discord.utils.oauth_url(user.id, permissions=INVITE_PERMISSIONS, scopes=("bot",))

    async def operate(self, operation, arguments, context) -> ToolResult:
        if self.client is None or self.client.user is None:
            return ToolResult.fail("Discord is not connected.", "not_connected")
        if operation == "invite":
            return ToolResult.ok(f"Invite link: {self.invite_url()}")
        conversation = context.address.conversation
        if operation == "time":
            return ToolResult.ok(clock_text())
        if operation == "clock_start":
            try:
                error = await self.clocks.start(conversation)
            except discord.HTTPException as failure:
                return ToolResult.fail(f"Could not start the clock: HTTP {getattr(failure, 'status', '?')}",
                                       "send_failed")
            return ToolResult.fail(error, "clock_unavailable") if error else ToolResult.ok("Starting Clock")
        if operation == "clock_stop":
            return ToolResult.ok("Stopping Timer" if await self.clocks.stop(conversation) else "No active clock to stop")
        if operation in ("music", "music_status", "music_control", "queue_music"):
            return await self._music(operation, arguments, context)
        if operation == "status":
            guilds = list(getattr(self.client, "guilds", []) or [])
            latency = getattr(self.client, "latency", float("nan"))
            names = ", ".join(guild.name for guild in guilds[:10]) or "none"
            return ToolResult.ok(f"Logged in as {self.client.user} ({self.client.user.id}); latency "
                                 f"{latency * 1000:.0f} ms; {len(guilds)} server(s): {names}; "
                                 f"application owners: {', '.join(self._owners) or 'unknown'}.")
        return await super().operate(operation, arguments, context)

    def _voice_channel(self, guild, sender_id: str):
        member = guild.get_member(int(sender_id)) if sender_id.isdigit() else None
        member = member or self._members.get((str(guild.id), sender_id))
        return getattr(getattr(member, "voice", None), "channel", None)

    async def _music(self, operation, arguments, context) -> ToolResult:
        conversation = context.address.conversation
        channel = None if conversation.startswith("dm:") else await self._channel(conversation)
        guild = getattr(channel, "guild", None)
        if guild is None:
            return ToolResult.fail("Music works in a server's text channel while you are in one of its voice "
                                   "channels.", "not_in_server")
        if operation == "music_status":
            command = MusicCommand("status")
        elif operation == "music_control":
            command = MusicCommand(CONTROL_ACTIONS[arguments["action"]])
        elif operation == "queue_music":
            url = str(arguments["url"]).strip()
            if not is_url(url):
                return ToolResult.fail("url must be an http(s) URL", "invalid_arguments")
            command = MusicCommand("youtube", [url], queue_only=not arguments.get("play_now", False))
        else:
            command = parse_music_command(str(arguments.get("command") or ""))
        return await self.music.run(command, guild, self._voice_channel(guild, context.sender.id))

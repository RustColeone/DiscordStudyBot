"""Discord interface: DMs and server channels as conversations.

Conversations are `dm:<user id>` for direct messages and `<channel id>` for server channels and
threads. In servers the bot answers when mentioned or when someone replies to one of its messages
(the core's admission rules decide; the interface only reports the facts).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any

import discord

from contracts import (Address, Delivery, Display, FileRef, Interface, Operation, Reply, Request, Sender,
                       ToolResult)

DEFAULT_UPLOAD_BYTES = 10 * 1024 * 1024
MESSAGE_LIMIT = 2000
INVITE_PERMISSIONS = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                         read_message_history=True, attach_files=True, embed_links=True,
                                         add_reactions=True)
NOTICES = {
    "run_failed": "⚠️ Sorry, I couldn't finish that.",
    "waiting_approval": "⏳ Waiting for the owner's approval (`{approval_id}`).",
    "unknown_command": "Unknown command `${command}`. Try `$help`.",
    "not_permitted": "That command isn't available here.",
}


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = 0
        for separator in ("\n\n", "\n", " "):
            boundary = remaining.rfind(separator, 0, limit + 1)
            if boundary >= limit // 2:
                cut = boundary + len(separator)
                break
        cut = cut or limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


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
    operations = (Operation("status", "Discord connection status, servers and owners", command="discord_status"),
                  Operation("invite", "The link for inviting this bot to a server", command="invite"))

    def __init__(self, token: str, data_dir: Path, attachment_max_mb: float = 25.0, client: Any = None):
        self.token = token
        self.attachments_dir = Path(data_dir) / "attachments"
        self.attachment_max_bytes = int(attachment_max_mb * 1024 * 1024)
        self.client = client
        self.port = None
        self._task: asyncio.Task | None = None
        self._channels: OrderedDict[str, Any] = OrderedDict()
        self._progress: OrderedDict[str, Any] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owners: list[str] = []

    # ---- lifecycle --------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        self._prune_attachments()
        if not self.token:
            print("[discord] No bot token: set DISCORD_TOKEN (or TOKEN) in .env. Discord is not connected.", flush=True)
            port.log("token_missing")
            port.set_ready(False, "No Discord bot token is configured.")
            return
        if self.client is None:
            intents = discord.Intents.default()
            intents.message_content = True
            self.client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self._task = asyncio.create_task(self._run(), name="discord-client")

    async def _run(self) -> None:
        try:
            await self.client.start(self.token)
        except discord.PrivilegedIntentsRequired:
            print("[discord] The Message Content intent is disabled. Enable it in the Discord Developer Portal "
                  "(Bot → Privileged Gateway Intents) and restart.", flush=True)
            self.port.set_ready(False, "Message Content intent is not enabled for this bot.")
        except discord.LoginFailure:
            print("[discord] The bot token was rejected. Check DISCORD_TOKEN.", flush=True)
            self.port.set_ready(False, "The Discord token was rejected.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"[discord] Connection stopped: {type(error).__name__}: {error}", flush=True)
            self.port.log("client_stopped", error=type(error).__name__)

    async def stop(self) -> None:
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
        async with self._locks[conversation]:
            request = await self.to_request(message, conversation)
            result = await self.port.submit(request)
        if result and result.reason == "accepted":
            try:
                await message.channel.typing()
            except Exception:
                pass

    def conversation_id(self, channel, author) -> str:
        if getattr(channel, "type", None) == discord.ChannelType.private:
            return f"dm:{author.id}"
        return str(channel.id)

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
        addressed = is_direct or mentioned or replied_to_me
        files, kinds = [], []
        for attachment in message.attachments or []:
            kinds.append(attachment_kind(getattr(attachment, "content_type", "")))
            if addressed and attachment.size <= self.attachment_max_bytes:
                files.append(await self._download(message.id, attachment))
        kind = kinds[0] if kinds else "text"
        if not content and kinds:
            content = " ".join(f"[{item}]" for item in kinds)
        limit = getattr(guild, "filesize_limit", DEFAULT_UPLOAD_BYTES) if guild else DEFAULT_UPLOAD_BYTES
        display = Display(markdown=True, edits=True, progress=True, quotes=True, files=True,
                          max_upload_mb=round(limit / 1024 / 1024, 2), max_text_chars=MESSAGE_LIMIT)
        name = getattr(message.author, "display_name", None) or message.author.name
        return Request(
            id=str(message.id), address=Address(self.name, conversation), sender=Sender(str(message.author.id), name),
            text=content, received_at=message.created_at.timestamp(), is_direct=is_direct,
            mentions_me=True if is_direct else (mentioned or replied_to_me),
            reply_to=str(reference.message_id) if reference and reference.message_id else None,
            quoted_text=(resolved_message.content or "")[:1000] if resolved_message is not None else "",
            kind=kind, files=tuple(files), display=display,
            extra={"guild_id": str(guild.id) if guild else "", "channel_name": getattr(channel, "name", "") or ""})

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

    def render(self, reply: Reply) -> str:
        data = dict(reply.data)
        if reply.kind == "notice":
            if reply.code == "relayed":
                return (f"↪️ Sent to `{data.get('address')}`." if data.get("ok") == "true"
                        else f"⚠️ Not sent to `{data.get('address')}`: {data.get('detail') or 'unknown error'}")
            if reply.code == "approval_request":
                return "🔐 " + reply.text
            if reply.code == "run_failed_detail":
                return "⚠️ " + reply.text
            if reply.code in NOTICES:
                return NOTICES[reply.code].format(**{"approval_id": "", "command": "", **data})
            return reply.text or f"[{reply.code}]"
        if reply.kind == "event":
            origin = data.get("origin_address", "")
            footer = f"\n-# {origin}" if origin and not origin.endswith(":_interface") else ""
            return f"📨 {reply.text}{footer}" if reply.text else ""
        origin = data.get("origin_address")
        return (f"-# from `{origin}`\n" if origin else "") + (reply.text or "")

    async def deliver(self, reply: Reply) -> Delivery:
        if self.client is None:
            return Delivery(False, detail="not_connected")
        channel = await self._channel(reply.address.conversation)
        if channel is None:
            return Delivery(False, detail="unknown_conversation")
        if reply.kind == "progress":
            return await self._show_progress(channel, reply)
        reference = None
        if reply.quote and str(reply.quote).isdigit():
            reference = discord.MessageReference(message_id=int(reply.quote), channel_id=channel.id,
                                                 fail_if_not_exists=False)
        text = self.render(reply)
        sent = []
        try:
            if reply.kind == "file":
                file = discord.File(reply.file.path, filename=reply.file.name or Path(reply.file.path).name)
                sent.append(await channel.send(content=text[:MESSAGE_LIMIT] or None, file=file, reference=reference))
            else:
                if not text.strip():
                    return Delivery(True, detail="suppressed")
                for index, chunk in enumerate(split_text(text)):
                    sent.append(await channel.send(content=chunk, reference=reference if index == 0 else None))
        except (discord.Forbidden, discord.NotFound) as error:
            return Delivery(False, detail=f"{type(error).__name__}: {error}"[:300], uncertain=bool(sent))
        except discord.HTTPException as error:
            certain = getattr(error, "status", 500) < 500 and not sent
            return Delivery(False, detail=f"HTTP {getattr(error, 'status', '?')}: {error}"[:300], uncertain=not certain)
        except (OSError, asyncio.TimeoutError) as error:
            return Delivery(False, detail=f"{type(error).__name__}: {error}"[:300], uncertain=True)
        await self._clear_progress(reply.run_id)
        return Delivery(True, message_id=str(sent[0].id))

    async def _show_progress(self, channel, reply: Reply) -> Delivery:
        key = reply.run_id or reply.in_reply_to or reply.address.conversation
        text = ("⏳ " + self.render(reply))[:MESSAGE_LIMIT]
        message = self._progress.get(key)
        try:
            if message is not None:
                await message.edit(content=text)
            else:
                message = await channel.send(content=text)
                self._progress[key] = message
                while len(self._progress) > 200:
                    self._progress.popitem(last=False)
        except discord.HTTPException as error:
            return Delivery(False, detail=f"HTTP {getattr(error, 'status', '?')}")
        return Delivery(True, message_id=str(message.id))

    async def _clear_progress(self, run_id: str | None) -> None:
        message = self._progress.pop(run_id, None) if run_id else None
        if message is not None:
            try:
                await message.delete()
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
        if operation == "status":
            guilds = list(getattr(self.client, "guilds", []) or [])
            latency = getattr(self.client, "latency", float("nan"))
            names = ", ".join(guild.name for guild in guilds[:10]) or "none"
            return ToolResult.ok(f"Logged in as {self.client.user} ({self.client.user.id}); latency "
                                 f"{latency * 1000:.0f} ms; {len(guilds)} server(s): {names}; "
                                 f"application owners: {', '.join(self._owners) or 'unknown'}.")
        return await super().operate(operation, arguments, context)

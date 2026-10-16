"""Whale-chan's own Discord bot (its own application and token). Deliberately small: DMs, mentions and
replies in servers, text with rendered math, files, image attachments and a progress line. Everything
else (policy, tools, models) belongs to the core."""

from __future__ import annotations

import asyncio
import dataclasses
from collections import OrderedDict
from pathlib import Path

import discord

from contracts import Address, Delivery, Display, FileRef, Interface, Reply, Request, Sender

from .math_render import MathRenderer, fallback_text, split_math

LIMIT = 2000
NOTICES = {
    "run_failed": "⚠️ Sorry, I couldn't finish that.",
    "waiting_approval": "⏳ This needs the owner's approval first.",
    "not_permitted": "I can't do that here.",
    "unknown_command": "Unknown command `${command}`. Try `$help`.",
    "budget_exceeded": "⚠️ That took more work than I'm allowed for one request.",
}


def split_text(text: str, limit: int = LIMIT) -> list[str]:
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = 0
        for separator in ("\n\n", "\n", " "):
            boundary = remaining.rfind(separator, 0, limit - len(separator) + 1)
            if boundary >= limit // 2:
                cut = boundary + len(separator)
                break
        chunks.append(remaining[:cut or limit])
        remaining = remaining[cut or limit:]
    return [*chunks, remaining] if remaining or not chunks else chunks


class WhaleChanBot(Interface):
    def __init__(self, name: str, token: str, data_dir: Path, attachment_max_mb: float = 25.0, config=None):
        self.name, self.token = name, token
        self.attachments = Path(data_dir) / "attachments"
        self.attachment_max = int(attachment_max_mb * 1024 * 1024)
        self.display = Display(markdown=True, edits=True, progress=True, quotes=True, files=True,
                               max_upload_mb=10, max_text_chars=LIMIT)
        self.operations = ()
        self.math = MathRenderer(Path(data_dir) / "math", config or {})
        self.client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._channels: dict[str, object] = {}
        self._progress: OrderedDict[str, discord.Message] = OrderedDict()

    async def start(self, port) -> None:
        self.port = port
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self._task = asyncio.create_task(self._run())

    def invite_url(self) -> str:
        """The bot's user ID is the token's first part (base64), which is also its application ID."""
        import base64
        head = self.token.split(".", 1)[0]
        try:
            client_id = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)).decode()
        except ValueError:
            return "https://discord.com/developers/applications"
        permissions = discord.Permissions(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                          read_message_history=True, attach_files=True, embed_links=True)
        return f"https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot&permissions={permissions.value}"

    def _not_ready(self, detail: str) -> None:
        print(f"[{self.name}] {detail}", flush=True)
        self.port.set_ready(False, detail)

    async def _run(self) -> None:
        try:
            await self.client.start(self.token)
        except discord.LoginFailure:
            self._not_ready("The bot token was rejected.")
        except discord.PrivilegedIntentsRequired:
            self._not_ready("Turn on Message Content Intent for this bot in the Discord Developer Portal "
                            "(Bot → Privileged Gateway Intents), then restart.")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._not_ready(f"Discord connection failed: {type(error).__name__}: {error}"[:300])

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def on_ready(self) -> None:
        try:
            application = await self.client.application_info()
            team = getattr(application, "team", None)
            self.port.set_owners([str(member.id) for member in team.members] if team else [str(application.owner.id)])
        except discord.HTTPException as error:
            self.port.log("owner_lookup_failed", error=type(error).__name__)
        self.port.set_ready(True, f"Logged in as {self.client.user}.")
        message = f"[{self.name}] Logged in as {self.client.user} in {len(self.client.guilds)} server(s)."
        print(message + ("" if self.client.guilds else f" Invite it: {self.invite_url()}"), flush=True)

    # ---- incoming ---------------------------------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        me = self.client.user
        if message.author.bot or (me is not None and message.author.id == me.id):
            return
        direct = isinstance(message.channel, discord.DMChannel)
        conversation = f"dm:{message.author.id}" if direct else str(message.channel.id)
        self._channels[conversation] = message.channel
        mentioned = me is not None and any(user.id == me.id for user in message.mentions)
        reference = message.reference
        resolved = getattr(reference, "resolved", None) if reference else None
        replied_to_me = isinstance(resolved, discord.Message) and me is not None and resolved.author.id == me.id
        text = (message.content or "").replace(f"<@{me.id}>", "").replace(f"<@!{me.id}>", "").strip() if me else message.content
        files = []
        if direct or mentioned or replied_to_me:
            for attachment in message.attachments:
                if attachment.size <= self.attachment_max:
                    path = self.attachments / f"{message.id}-{attachment.filename}"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await attachment.save(path)
                    files.append(FileRef(str(path), attachment.filename, attachment.content_type or "", attachment.size))
        if not text and files:
            text = " ".join(f"[{file.name}]" for file in files)
        limit = message.guild.filesize_limit if message.guild else 10 * 1024 * 1024
        await self.port.submit(Request(
            id=str(message.id), address=Address(self.name, conversation),
            sender=Sender(str(message.author.id), message.author.display_name), text=text,
            received_at=message.created_at.timestamp(), is_direct=direct,
            mentions_me=True if direct else (mentioned or replied_to_me),
            reply_to=str(reference.message_id) if reference and reference.message_id else None,
            quoted_text=(resolved.content or "")[:1000] if isinstance(resolved, discord.Message) else "",
            files=tuple(files), display=dataclasses.replace(self.display, max_upload_mb=round(limit / 1024 ** 2, 2))))

    # ---- outgoing ---------------------------------------------------------------------------------
    async def _channel(self, conversation: str):
        if conversation in self._channels:
            return self._channels[conversation]
        try:
            if conversation.startswith("dm:"):
                user = await self.client.fetch_user(int(conversation[3:]))
                channel = user.dm_channel or await user.create_dm()
            else:
                channel = self.client.get_channel(int(conversation)) or await self.client.fetch_channel(int(conversation))
        except (ValueError, discord.HTTPException):
            return None
        self._channels[conversation] = channel
        return channel

    def render(self, reply: Reply) -> str:
        data = dict(reply.data)
        if reply.kind == "notice":
            if reply.code in NOTICES:
                return NOTICES[reply.code].replace("${command}", data.get("command", ""))
            return reply.text or f"[{reply.code}]"
        speaker = f"**{reply.speaker}**\n" if reply.speaker else ""
        return speaker + (reply.text or "")

    async def deliver(self, reply: Reply) -> Delivery:
        if self.client is None or not self.client.is_ready():
            return Delivery(False, detail="not_connected")
        channel = await self._channel(reply.address.conversation)
        if channel is None:
            return Delivery(False, detail="unknown_conversation")
        try:
            if reply.kind == "progress":
                return await self._show_progress(channel, reply)
            if reply.run_id and reply.kind in ("text", "file", "notice"):
                await self._clear_progress(reply.run_id)
            reference = (discord.MessageReference(message_id=int(reply.quote), channel_id=channel.id,
                                                  fail_if_not_exists=False)
                         if reply.quote and str(reply.quote).isdigit() else None)
            if reply.kind == "file":
                sent = await channel.send(content=reply.text[:LIMIT] or None, reference=reference,
                                          file=discord.File(reply.file.path, filename=reply.file.name or None))
                return Delivery(True, message_id=str(sent.id))
            text = self.render(reply)
            if not text.strip():
                return Delivery(True, detail="suppressed")
            first = None
            for kind, value in await self._parts(reply, text):
                if kind == "image":
                    try:
                        sent = await channel.send(file=discord.File(value, filename="formula.png"),
                                                  reference=reference if first is None else None)
                    finally:
                        Path(value).unlink(missing_ok=True)
                else:
                    sent = await channel.send(content=value, reference=reference if first is None else None)
                first = first or sent
            return Delivery(True, message_id=str(first.id))
        except (discord.Forbidden, discord.NotFound) as error:
            return Delivery(False, detail=type(error).__name__)
        except discord.HTTPException as error:
            return Delivery(False, detail=f"HTTP {error.status}", uncertain=error.status >= 500)

    async def _parts(self, reply: Reply, text: str) -> list[tuple[str, str]]:
        """Text chunks and rendered display-math images, in order."""
        if reply.kind != "text" or not self.math.enabled or "$" not in text and "\\" not in text:
            return [("text", chunk) for chunk in split_text(text)]
        parts, images = [], 0
        for kind, value in split_math(text):
            if kind == "text":
                parts.extend(("text", chunk) for chunk in split_text(value) if value.strip())
                continue
            path = None
            if images < self.math.max_images:
                try:
                    path = await asyncio.to_thread(self.math.render, value)
                    images += 1
                except Exception as error:
                    self.port.log("math_render_failed", error=type(error).__name__)
            parts.extend([("image", str(path))] if path else [("text", chunk) for chunk in split_text(fallback_text(value))])
        return parts or [("text", chunk) for chunk in split_text(text)]

    async def _show_progress(self, channel, reply: Reply) -> Delivery:
        key = reply.run_id or reply.address.conversation
        text = f"-# ⏳ {reply.text}"[:LIMIT]
        message = self._progress.get(key)
        if message is not None:
            await message.edit(content=text)
        else:
            message = self._progress[key] = await channel.send(content=text)
            while len(self._progress) > 100:
                self._progress.popitem(last=False)
        return Delivery(True, message_id=str(message.id))

    async def _clear_progress(self, run_id: str) -> None:
        message = self._progress.pop(run_id, None)
        if message is not None:
            try:
                await message.delete()
            except discord.HTTPException:
                pass

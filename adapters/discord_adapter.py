import asyncio
import os
import re
import traceback
from collections import OrderedDict

import discord

from app.bot_types import BotResponse, IncomingMessage
from services.command_queue import CommandQueue, QueuedCommand
from services.attachment_store import attachment_metadata
from services import rich_content
from services.vision import image_metadata
from services.request_timing import RequestTiming


class _ManagedDiscordClient(discord.Client):
    def __init__(self, app, **options):
        self.app = app
        super().__init__(**options)

    async def close(self) -> None:
        await self.app.stop_background_tasks()
        await super().close()


class DiscordAdapter:
    supports_progress = True
    supports_message_edit = True
    supports_voice = True
    supports_live_responses = True

    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.allowed_channel_ids = self._parse_allowed_channel_ids(config)
        self.require_mention = self._parse_bool(config.get("DISCORD_REQUIRE_MENTION", False))
        self.creator_user_id = str(config.get("CREATOR_USER_ID", "")).strip()
        self.creator_only_mode = self._parse_bool(config.get("CREATOR_ONLY_MODE", False))
        self.command_queue = CommandQueue(max_waiting=5)
        self.command_worker_task = None
        self.recent_channels = OrderedDict()
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = _ManagedDiscordClient(app, intents=intents)
        self.app.attach_platform(self)
        self._register_events()

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready():
            print(f"We have logged in as {self.client.user}")
            await self.app.start_background_tasks()
            self._start_command_worker()

        @self.client.event
        async def on_message(native_message):
            if not self._is_allowed_message(native_message):
                return
            incoming_message = self._to_incoming_message(native_message)
            if incoming_message.content == "$admin" or incoming_message.content.startswith("$admin "):
                responses = await self.app.handle_message(incoming_message)
                await self._send_responses(native_message.channel, responses, incoming_message.metadata.get("request_timing"))
                return
            if incoming_message.content.startswith("$"):
                await self._enqueue_command(native_message.channel, incoming_message)
                return
            responses = await self.app.handle_message(incoming_message)
            await self._send_responses(native_message.channel, responses, incoming_message.metadata.get("request_timing"))

    def _start_command_worker(self) -> None:
        if self.command_worker_task is None or self.command_worker_task.done():
            self.command_worker_task = self.client.loop.create_task(self._run_command_worker())

    async def _enqueue_command(self, channel, message: IncomingMessage) -> None:
        timing = message.metadata.setdefault("request_timing", RequestTiming())
        timing.record("queued", ahead=self.command_queue.waiting, active=self.command_queue.active)
        command = QueuedCommand(message, channel, bool(message.metadata.get("is_creator")))
        accepted, _ = self.command_queue.submit(command)
        if not accepted:
            await channel.send("Command queue is full (5 waiting); your command was dropped.")

    async def _run_command_worker(self) -> None:
        while True:
            command = await self.command_queue.get()
            self.command_queue.active = True
            timing = command.message.metadata.get("request_timing")
            if timing is not None:
                timing.record("dequeued")
            try:
                responses = await self.app.handle_message(command.message)
                await self._send_responses(command.channel, responses, timing)
            except Exception as error:
                print(
                    "Queued command failed "
                    f"channel={command.message.channel_id} "
                    f"author={command.message.author_id}: {error}",
                    flush=True,
                )
                traceback.print_exc()
                await command.channel.send("Command failed. Check the bot logs for details.")
            finally:
                self.command_queue.active = False
                if timing is not None:
                    timing.record("command_finished")

    @staticmethod
    def _parse_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _parse_allowed_channel_ids(config) -> set[str]:
        raw_value = config.get("DISCORD_ALLOWED_CHANNELS", "")
        if isinstance(raw_value, (list, tuple, set)):
            values = raw_value
        else:
            values = str(raw_value).split(",")
        return {str(value).strip() for value in values if str(value).strip()}

    def _is_allowed_message(self, native_message) -> bool:
        if bool(getattr(native_message.author, "bot", False)):
            return False
        if self.creator_only_mode and str(native_message.author.id) != self.creator_user_id:
            return False
        if self.allowed_channel_ids and str(native_message.channel.id) not in self.allowed_channel_ids:
            return False
        if self.require_mention and self.client.user not in getattr(native_message, "mentions", []):
            return False
        return True

    def _to_incoming_message(self, native_message) -> IncomingMessage:
        guild = native_message.guild
        channel = native_message.channel
        # DM channels are not guaranteed to be present in Client.get_channel().
        if not hasattr(self, "recent_channels"):
            self.recent_channels = OrderedDict()
        self.recent_channels[str(channel.id)] = channel
        self.recent_channels.move_to_end(str(channel.id))
        while len(self.recent_channels) > 128:
            self.recent_channels.popitem(last=False)
        author = native_message.author
        is_dm = isinstance(channel, discord.DMChannel)

        content = native_message.content
        was_mentioned = self.client.user in getattr(native_message, "mentions", [])
        is_creator = bool(self.creator_user_id) and str(author.id) == self.creator_user_id
        reference = getattr(native_message, "reference", None)
        referenced_message = getattr(reference, "resolved", None)
        referenced_content = getattr(referenced_message, "content", "") or ""
        referenced_attachments = [
            metadata for attachment in getattr(referenced_message, "attachments", [])
            if (metadata := attachment_metadata(
                attachment,
                getattr(referenced_message, "id", None),
                getattr(getattr(referenced_message, "author", None), "id", None),
                getattr(referenced_message, "created_at", None),
            )) is not None
        ]
        referenced_attachment_urls = [
            attachment["url"] for attachment in referenced_attachments
        ]
        referenced_image_attachments = [
            image for attachment in getattr(referenced_message, "attachments", [])
            if (image := image_metadata(attachment)) is not None
        ]
        attachments = [
            metadata for attachment in getattr(native_message, "attachments", [])
            if (metadata := attachment_metadata(
                attachment,
                getattr(native_message, "id", None),
                getattr(author, "id", None),
                getattr(native_message, "created_at", None),
            )) is not None
        ]
        attachment_urls = [attachment["url"] for attachment in attachments]
        image_attachments = [
            image for attachment in getattr(native_message, "attachments", [])
            if (image := image_metadata(attachment)) is not None
        ]
        if was_mentioned and self.client.user is not None:
            content = re.sub(rf"<@!?{self.client.user.id}>\s*", "", content).strip()
            if content and not content.startswith("$"):
                content = f"$agent {content}"

        return IncomingMessage(
            content=content,
            channel_id=str(channel.id),
            author_id=str(author.id),
            author_name=author.name,
            author_display_name=getattr(author, "display_name", author.name),
            created_at=native_message.created_at,
            author_is_bot=bool(getattr(author, "bot", False)),
            guild_name=guild.name if guild else None,
            channel_name=getattr(channel, "name", None),
            is_dm=is_dm,
            metadata={
                "platform": "discord",
                "native_message": native_message,
                "native_channel": channel,
                "native_client": self.client,
                "author_ref": author,
                "guild_ref": guild,
                "guild_premium_tier": getattr(guild, "premium_tier", 0) if guild else 0,
                "was_mentioned": was_mentioned,
                "is_creator": is_creator,
                "referenced_content": referenced_content,
                'referenced_message_id':str(getattr(reference,'message_id',None) or getattr(referenced_message,'id','') or ''),
                "referenced_attachments": referenced_attachments,
                "referenced_attachment_urls": referenced_attachment_urls,
                "referenced_image_attachments": referenced_image_attachments,
                "attachments": attachments,
                "attachment_urls": attachment_urls,
                "image_attachments": image_attachments,
            },
        )

    async def _send_responses(self, channel, responses, timing=None):
        for response in responses:
            if response.metadata.get("sent_live"):
                continue
            for rendered_response in rich_content.expand_response(response):
                sent = await self._send_response(channel, rendered_response)
                if sent is not None and timing is not None:
                    timing.delivered()

    async def _send_response(self, channel, response):
        first_message = None
        if response.text is not None and response.attachment is None:
            for chunk in self._split_message(response.text):
                sent_message = await channel.send(
                    chunk,
                    suppress_embeds=bool(response.metadata.get("suppress_embeds")),
                )
                if first_message is None:
                    first_message = sent_message
            return first_message

        if response.attachment is not None:
            return await self._send_attachment_response(channel, response)
        return None

    @staticmethod
    def _split_message(content: str, limit: int = 2000) -> list[str]:
        chunks = []
        remaining = content
        while len(remaining) > limit:
            split_at = 0
            for separator in ("\n\n", "\n", " "):
                boundary = remaining.rfind(separator, 0, limit + 1)
                if boundary >= limit // 2:
                    split_at = boundary + len(separator)
                    break
            if not split_at:
                split_at = limit
            chunks.append(remaining[:split_at])
            remaining = remaining[split_at:]
        chunks.append(remaining)
        return chunks

    async def _send_attachment_response(self, channel, response) -> None:
        attachment = response.attachment
        file_name = attachment.filename or os.path.basename(attachment.path)
        attempts = 3
        last_error = None
        uploaded = False

        try:
            for attempt in range(1, attempts + 1):
                try:
                    discord_file = discord.File(attachment.path, filename=file_name)
                    sent_message = await channel.send(
                        response.text or None,
                        file=discord_file,
                        suppress_embeds=bool(response.metadata.get("suppress_embeds")),
                    )
                    uploaded = True
                    return sent_message
                except Exception as error:
                    last_error = error
                    print(f"Attachment upload failed (attempt {attempt}/{attempts}) for {file_name}: {error}")
                    if attempt < attempts:
                        await asyncio.sleep(attempt)

            response.metadata["delivery_failed"] = True
            fallback_text = response.text or f"⚠️ Failed to upload attachment: {file_name}"
            await channel.send(
                f"{fallback_text}\n⚠️ Attachment upload failed after {attempts} attempts: {last_error}\n"
                "The rendered file was retained on the bot host instead of being deleted."
            )
            return None
        finally:
            if uploaded and attachment.delete_after_send and os.path.exists(attachment.path):
                os.remove(attachment.path)

    async def send_channel_message(self, channel_id: str, content: str):
        channel = await self._resolve_channel(channel_id)
        first_message = None
        for chunk in self._split_message(content):
            sent_message = await channel.send(chunk)
            if first_message is None:
                first_message = sent_message
        return first_message

    async def send_channel_response(self, channel_id: str, response: BotResponse):
        channel = await self._resolve_channel(channel_id)
        return await self._send_response(channel, response)

    async def _resolve_channel(self, channel_id):
        channel = getattr(self, "recent_channels", {}).get(str(channel_id))
        if channel is None:
            channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
            if not hasattr(self, "recent_channels"):
                self.recent_channels = OrderedDict()
            self.recent_channels[str(channel_id)] = channel
            while len(self.recent_channels) > 128:
                self.recent_channels.popitem(last=False)
        return channel

    async def edit_message(self, native_message, content: str) -> None:
        await native_message.edit(content=content)

    async def delete_message(self, native_message) -> None:
        await native_message.delete()

    async def send_direct_message(self, user_reference, content: str):
        if isinstance(user_reference,(str,int)):
            user_id=int(user_reference)
            user_reference=self.client.get_user(user_id) or await self.client.fetch_user(user_id)
        first=None
        for chunk in self._split_message(content):
            sent=await user_reference.send(chunk)
            if first is None: first=sent
        return first

    def create_background_task(self, coroutine):
        return self.client.loop.create_task(coroutine)

    def run(self):
        self.client.run(self.config["TOKEN"])

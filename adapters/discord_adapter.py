import asyncio
import os
import re

import discord

from app.bot_types import IncomingMessage
from services.command_queue import CommandQueue, QueuedCommand


class DiscordAdapter:
    supports_progress = True
    supports_message_edit = True
    supports_voice = True

    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.allowed_channel_ids = self._parse_allowed_channel_ids(config)
        self.require_mention = self._parse_bool(config.get("DISCORD_REQUIRE_MENTION", False))
        self.creator_user_id = str(config.get("CREATOR_USER_ID", "")).strip()
        self.creator_only_mode = self._parse_bool(config.get("CREATOR_ONLY_MODE", False))
        self.command_queue = CommandQueue(max_waiting=5)
        self.command_worker_task = None
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents)
        self.app.attach_platform(self)
        self._register_events()

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready():
            print(f"We have logged in as {self.client.user}")
            self.app.start_background_tasks()
            self._start_command_worker()

        @self.client.event
        async def on_message(native_message):
            if not self._is_allowed_message(native_message):
                return
            incoming_message = self._to_incoming_message(native_message)
            if incoming_message.content == "$admin" or incoming_message.content.startswith("$admin "):
                responses = await self.app.handle_message(incoming_message)
                await self._send_responses(native_message.channel, responses)
                return
            if incoming_message.content.startswith("$"):
                await self._enqueue_command(native_message.channel, incoming_message)
                return
            async with native_message.channel.typing():
                responses = await self.app.handle_message(incoming_message)
                await self._send_responses(native_message.channel, responses)

    def _start_command_worker(self) -> None:
        if self.command_worker_task is None or self.command_worker_task.done():
            self.command_worker_task = self.client.loop.create_task(self._run_command_worker())

    async def _enqueue_command(self, channel, message: IncomingMessage) -> None:
        command = QueuedCommand(message, channel, bool(message.metadata.get("is_creator")))
        accepted, _ = self.command_queue.submit(command)
        if not accepted:
            await channel.send("Command queue is full (5 waiting); your command was dropped.")

    async def _run_command_worker(self) -> None:
        while True:
            command = await self.command_queue.get()
            self.command_queue.active = True
            try:
                async with command.channel.typing():
                    responses = await self.app.handle_message(command.message)
                    await self._send_responses(command.channel, responses)
            except Exception as error:
                print(f"Queued command failed: {error}")
                await command.channel.send("Command failed. Check the bot logs for details.")
            finally:
                self.command_queue.active = False

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
        author = native_message.author
        is_dm = isinstance(channel, discord.DMChannel)

        content = native_message.content
        was_mentioned = self.client.user in getattr(native_message, "mentions", [])
        is_creator = bool(self.creator_user_id) and str(author.id) == self.creator_user_id
        reference = getattr(native_message, "reference", None)
        referenced_message = getattr(reference, "resolved", None)
        referenced_content = getattr(referenced_message, "content", "") or ""
        referenced_attachment_urls = [
            attachment.url for attachment in getattr(referenced_message, "attachments", [])
            if getattr(attachment, "url", None)
        ]
        attachment_urls = [
            attachment.url for attachment in getattr(native_message, "attachments", [])
            if getattr(attachment, "url", None)
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
                "native_message": native_message,
                "native_channel": channel,
                "native_client": self.client,
                "author_ref": author,
                "guild_ref": guild,
                "guild_premium_tier": getattr(guild, "premium_tier", 0) if guild else 0,
                "was_mentioned": was_mentioned,
                "is_creator": is_creator,
                "referenced_content": referenced_content,
                "referenced_attachment_urls": referenced_attachment_urls,
                "attachment_urls": attachment_urls,
            },
        )

    async def _send_responses(self, channel, responses):
        for response in responses:
            if response.text is not None and response.attachment is None:
                for chunk in self._split_message(response.text):
                    await channel.send(chunk)
                continue

            if response.attachment is not None:
                await self._send_attachment_response(channel, response)

    @staticmethod
    def _split_message(content: str, limit: int = 2000) -> list[str]:
        return [content[index:index + limit] for index in range(0, len(content), limit)] or [""]

    async def _send_attachment_response(self, channel, response) -> None:
        attachment = response.attachment
        file_name = attachment.filename or os.path.basename(attachment.path)
        attempts = 3
        last_error = None

        try:
            for attempt in range(1, attempts + 1):
                try:
                    discord_file = discord.File(attachment.path, filename=file_name)
                    await channel.send(response.text or None, file=discord_file)
                    return
                except Exception as error:
                    last_error = error
                    print(f"Attachment upload failed (attempt {attempt}/{attempts}) for {file_name}: {error}")
                    if attempt < attempts:
                        await asyncio.sleep(attempt)

            fallback_text = response.text or f"⚠️ Failed to upload attachment: {file_name}"
            await channel.send(f"{fallback_text}\n⚠️ Attachment upload failed after {attempts} attempts: {last_error}")
        finally:
            if attachment.delete_after_send and os.path.exists(attachment.path):
                os.remove(attachment.path)

    async def send_channel_message(self, channel_id: str, content: str):
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            raise ValueError(f"Channel {channel_id} was not found")
        return await channel.send(content)

    async def edit_message(self, native_message, content: str) -> None:
        await native_message.edit(content=content)

    async def delete_message(self, native_message) -> None:
        await native_message.delete()

    async def send_direct_message(self, user_reference, content: str) -> None:
        await user_reference.send(content)

    def create_background_task(self, coroutine):
        return self.client.loop.create_task(coroutine)

    def run(self):
        self.client.run(self.config["TOKEN"])

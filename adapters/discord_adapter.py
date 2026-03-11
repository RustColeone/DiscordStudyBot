import asyncio
import os

import discord

from app.bot_types import IncomingMessage


class DiscordAdapter:
    def __init__(self, app, config):
        self.app = app
        self.config = config
        self.client = discord.Client(intents=discord.Intents.all())
        self.app.attach_platform(self)
        self._register_events()

    def _register_events(self) -> None:
        @self.client.event
        async def on_ready():
            print(f"We have logged in as {self.client.user}")

        @self.client.event
        async def on_message(native_message):
            incoming_message = self._to_incoming_message(native_message)
            responses = await self.app.handle_message(incoming_message)
            await self._send_responses(native_message.channel, responses)

    def _to_incoming_message(self, native_message) -> IncomingMessage:
        guild = native_message.guild
        channel = native_message.channel
        author = native_message.author
        is_dm = isinstance(channel, discord.DMChannel)

        return IncomingMessage(
            content=native_message.content,
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
            },
        )

    async def _send_responses(self, channel, responses):
        for response in responses:
            if response.text is not None and response.attachment is None:
                await channel.send(response.text)
                continue

            if response.attachment is not None:
                await self._send_attachment_response(channel, response)

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

    async def send_direct_message(self, user_reference, content: str) -> None:
        await user_reference.send(content)

    def create_background_task(self, coroutine):
        return self.client.loop.create_task(coroutine)

    def run(self):
        self.client.run(self.config["TOKEN"])

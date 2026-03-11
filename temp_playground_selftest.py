import asyncio
import argparse
import os
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import discord

from app.bot_app import BotApplication
from app.bot_types import BotAttachment, BotResponse, IncomingMessage
from services.config_service import load_config

CLIP_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
MUSIC_TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@dataclass
class TestResult:
    label: str
    command: str
    status: str
    detail: str = ""


class ProxyAuthor:
    def __init__(self, user_id: str, name: str, display_name: str, log_channel, voice_channel=None):
        self.id = int(user_id)
        self.name = name
        self.display_name = display_name
        self.bot = False
        self.voice = type("VoiceState", (), {"channel": voice_channel})() if voice_channel else None
        self._log_channel = log_channel

    async def send(self, content: str):
        await self._log_channel.send(f"[SELFTEST DM] {content}")


class PlaygroundPlatform:
    def __init__(self, client: discord.Client):
        self.client = client

    async def send_channel_message(self, channel_id: str, content: str):
        channel = self.client.get_channel(int(channel_id)) or await self.client.fetch_channel(int(channel_id))
        return await channel.send(content)

    async def edit_message(self, native_message, content: str) -> None:
        await native_message.edit(content=content)

    async def send_direct_message(self, user_reference, content: str) -> None:
        if hasattr(user_reference, "send"):
            await user_reference.send(content)
            return
        user = self.client.get_user(int(user_reference)) or await self.client.fetch_user(int(user_reference))
        await user.send(content)

    def create_background_task(self, coroutine):
        return asyncio.create_task(coroutine)


class SelfTestClient(discord.Client):
    def __init__(self, config, playground_channel_id: str, voice_channel_id: Optional[str] = None):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.config = config
        self.playground_channel_id = playground_channel_id
        self.voice_channel_id = voice_channel_id
        self.enable_voice_tests = bool(voice_channel_id)
        self.results: List[TestResult] = []
        self.platform = PlaygroundPlatform(self)
        self.app = BotApplication(config)
        self.app.attach_platform(self.platform)
        self._ran = False
        self._export_path = os.path.join(os.getcwd(), "bot_data_export.json")

    async def on_ready(self):
        if self._ran:
            return
        self._ran = True
        print(f"Self-test logged in as {self.user} ({self.user.id})")
        await self.run_selftest()

    async def run_selftest(self):
        channel = self.get_channel(int(self.playground_channel_id)) or await self.fetch_channel(int(self.playground_channel_id))
        guild = getattr(channel, "guild", None)
        voice_channel = None
        if self.enable_voice_tests and self.voice_channel_id:
            voice_channel = self.get_channel(int(self.voice_channel_id)) or await self.fetch_channel(int(self.voice_channel_id))

        author = ProxyAuthor(
            user_id=str(self.user.id),
            name=self.user.name,
            display_name=getattr(self.user, "display_name", self.user.name),
            log_channel=channel,
            voice_channel=voice_channel,
        )

        await channel.send(
            "🧪 Starting playground self-test. Commands are mirrored into this channel, then injected into the app because bot-authored messages are ignored by design."
        )

        try:
            await self._run_sequence(channel, guild, author)
        finally:
            await self._cleanup(channel)
            summary = self._build_summary()
            print(summary)
            await self._send_summary(channel, summary)
            await channel.send("🧪 Playground self-test finished.")
            await self.close()

    async def _run_sequence(self, channel, guild, author):
        await self._run_step(channel, guild, author, "$help", "help")
        await self._run_step(channel, guild, author, "$help chat", "help-chat")
        await self._run_step(channel, guild, author, "$time", "time")
        await self._run_step(channel, guild, author, "$start", "clock-start")
        await asyncio.sleep(3)
        await self._run_step(channel, guild, author, "$stop", "clock-stop")
        await self._run_step(channel, guild, author, "$remindMeIn -t 0.02 -m playground self test", "reminder")
        await asyncio.sleep(2)

        await self._run_step(channel, guild, author, "$db -s", "db-stats")
        await self._run_step(channel, guild, author, "$db -e", "db-export")
        await self._run_step(channel, guild, author, "$db -i", "db-import")

        await self._run_step(channel, guild, author, "$bridge --status", "bridge-status")
        await self._run_step(channel, guild, author, "$bridge --init", "bridge-init")
        await self._run_step(channel, guild, author, "$bridge --send \"playground ping\"", "bridge-send")
        await self._run_step(channel, guild, author, "$bridge --listen on", "bridge-listen-on")
        await self._run_step(channel, guild, author, "bridge passive ping", "bridge-passive")
        await self._run_step(channel, guild, author, "$bridge --listen off", "bridge-listen-off")
        await self._run_step(channel, guild, author, "$bridge --disconnect", "bridge-disconnect")

        await self._run_step(channel, guild, author, "$chat", "chat-status")
        await self._run_step(channel, guild, author, "$chat --prompt list", "chat-prompt-list")
        await self._run_step(channel, guild, author, "$chat --prompt show", "chat-prompt-show")
        await self._run_step(channel, guild, author, "$chat -p 0", "chat-prompt-set")
        await self._run_step(channel, guild, author, "$chat --models", "chat-models")
        await self._run_step(channel, guild, author, "$chat --clear", "chat-clear")
        await self._run_step(channel, guild, author, "$chat --listen on", "chat-listen-on")
        await self._run_step(channel, guild, author, "Hello from the playground self test.", "chat-passive")
        await self._run_step(channel, guild, author, "$chat --listen off", "chat-listen-off")
        await self._run_step(channel, guild, author, "$chat --send \"Say chat smoke test ok.\"", "chat-send")

        await self._run_step(channel, guild, author, "$google -s \"Discord bot self test\"", "google")
        await self._run_step(channel, guild, author, "$wolfram -q \"2+2\"", "wolfram")

        await self._run_step(
            channel,
            guild,
            author,
            f"$clip -u \"{CLIP_TEST_URL}\" -s 0 -e 3",
            "clip-preview",
        )
        await self._run_step(channel, guild, author, "$clip --confirm", "clip-confirm")
        await self._run_step(channel, guild, author, "$clip --cancel", "clip-cancel")

        await self._run_step(channel, guild, author, "$music --name", "music-name")
        await self._run_step(channel, guild, author, "$music --init", "music-init")
        await self._run_step(channel, guild, author, "$music --play", "music-play")
        await asyncio.sleep(2)
        await self._run_step(channel, guild, author, "$music --pause", "music-pause")
        await self._run_step(channel, guild, author, "$music --next", "music-next")
        await self._run_step(channel, guild, author, "$music --prev", "music-prev")
        await self._run_step(channel, guild, author, f"$music -y \"{MUSIC_TEST_URL}\" --queue", "music-youtube")
        await self._run_step(channel, guild, author, "$music --stop", "music-stop")

        await self._run_step(channel, guild, author, "$broadcast playground self test", "broadcast")

    async def _run_step(self, channel, guild, author, content: str, label: str):
        print(f"\n=== {label}: {content}")
        await channel.send(f"```text\n[SELFTEST INPUT] {content}\n```")

        incoming = IncomingMessage(
            content=content,
            channel_id=str(channel.id),
            author_id=str(author.id),
            author_name=author.name,
            author_display_name=author.display_name,
            created_at=datetime.now(timezone.utc),
            author_is_bot=False,
            guild_name=guild.name if guild else None,
            channel_name=getattr(channel, "name", None),
            is_dm=False,
            metadata={
                "native_message": None,
                "native_channel": channel,
                "native_client": self,
                "author_ref": author,
                "guild_ref": guild,
                "guild_premium_tier": getattr(guild, "premium_tier", 0) if guild else 0,
            },
        )

        try:
            responses = await self.app.handle_message(incoming)
            await self._emit_responses(channel, responses)
            detail = self._summarize_responses(responses)
            status = "ok"
            if any((response.text or "").startswith(("❌", "⚠️")) for response in responses):
                status = "warn"
            self.results.append(TestResult(label=label, command=content, status=status, detail=detail))
        except Exception as error:
            traceback.print_exc()
            detail = f"{type(error).__name__}: {error}"
            await channel.send(f"❌ SELFTEST EXCEPTION during {label}: {detail}")
            self.results.append(TestResult(label=label, command=content, status="fail", detail=detail))

    async def _emit_responses(self, channel, responses: List[BotResponse]):
        if not responses:
            await channel.send("[SELFTEST] No response returned.")
            return

        for response in responses:
            if response.text is not None and response.attachment is None:
                await channel.send(response.text)
                continue

            if response.attachment is not None:
                file_name = response.attachment.filename or os.path.basename(response.attachment.path)
                try:
                    discord_file = discord.File(response.attachment.path, filename=file_name)
                    await channel.send(response.text or None, file=discord_file)
                except Exception as error:
                    await channel.send(f"⚠️ Attachment upload failed for {file_name}: {error}")
                finally:
                    if response.attachment.delete_after_send and os.path.exists(response.attachment.path):
                        os.remove(response.attachment.path)

    def _summarize_responses(self, responses: List[BotResponse]) -> str:
        if not responses:
            return "no response"

        summary_parts: List[str] = []
        for response in responses[:2]:
            if response.text:
                compact = response.text.replace("\n", " ")[:110]
                summary_parts.append(compact)
            elif response.attachment:
                summary_parts.append(f"attachment:{response.attachment.filename or os.path.basename(response.attachment.path)}")
        return " | ".join(summary_parts)[:180]

    def _build_summary(self) -> str:
        lines = ["Playground self-test summary", "-"]
        for result in self.results:
            lines.append(f"[{result.status.upper():4}] {result.label}: {result.detail}")
        return "\n".join(lines)

    async def _send_summary(self, channel, summary: str):
        chunk_size = 1800
        for index in range(0, len(summary), chunk_size):
            chunk = summary[index:index + chunk_size]
            await channel.send(f"```text\n{chunk}\n```")

    async def _cleanup(self, channel):
        if os.path.exists(self._export_path):
            try:
                os.remove(self._export_path)
                await channel.send("[SELFTEST CLEANUP] Removed bot_data_export.json")
            except OSError as error:
                await channel.send(f"[SELFTEST CLEANUP] Failed to remove bot_data_export.json: {error}")


def main():
    parser = argparse.ArgumentParser(description="Run the Discord playground self-test against a specified channel.")
    parser.add_argument("--channel-id", required=True, help="Text channel ID used as the self-test playground")
    parser.add_argument("--voice-channel-id", help="Voice channel ID for music/voice-path tests")
    args = parser.parse_args()

    config = load_config()
    config["ID_CHANNEL1"] = args.channel_id
    if args.voice_channel_id:
        config["ID_VOICECHANNEL"] = args.voice_channel_id
    client = SelfTestClient(config, playground_channel_id=args.channel_id, voice_channel_id=args.voice_channel_id)
    client.run(config["TOKEN"])


if __name__ == "__main__":
    main()

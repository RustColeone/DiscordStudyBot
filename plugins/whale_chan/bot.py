"""Whale-chan's own Discord bot (its own application and token). Deliberately small: DMs, mentions and
replies in servers, text with rendered math, files, image attachments and a progress line. Everything
else (policy, tools, models) belongs to the core."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import shutil
import subprocess
import time
from collections import OrderedDict
from pathlib import Path

import discord

from contracts import Address, Command, Delivery, Display, FileRef, Interface, Option, Reply, Request, Sender

from . import slash
from .math_render import MathRenderer, fallback_text, split_math
from .speakup import MARK, Ledger, Settings, SpeakUp
from .voice import VOICE_NAME

# Her own commands become plain messages her engine understands (`/speakup` the bot answers itself); the rest
# come from the core's catalog.
LOCAL = {
    "new": (Command("new", "Start a fresh chat (earlier messages stop being context)"), "/new"),
    "voice": (Command("voice", "Her replies here as voice messages: on or off",
                      (Option("mode", "on or off", choices=("on", "off")),)), "/voice {mode}"),
    "speakup": (Command("speakup", "鲸酱 messaging first: status, rehearse, now, on, off or dry (owner only)",
                        (Option("do", "what to do", choices=("status", "rehearse", "now", "on", "off", "dry")),),
                        access="creator"), None),
}
SLASH_DEFAULT = ("help", "memory", "remind", "reminders", "routine", "draw", "search", "read", "video")

LIMIT = 2000
NOTICES = {
    "run_failed": "⚠️ Sorry, I couldn't finish that.",
    "waiting_approval": "⏳ This needs the owner's approval first.",
    "not_permitted": "I can't do that here.",
    "unknown_command": "Unknown command `${command}`. Try `$help`.",
    "budget_exceeded": "⚠️ That took more work than I'm allowed for one request.",
    "locked": "🔧 本鲸在维护中，请稍后再来～",
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


class VoiceFile(discord.File):
    """An attachment that Discord shows as a voice message: it needs its length and a waveform."""

    def __init__(self, path: Path, duration: float, waveform: str):
        super().__init__(str(path), filename=VOICE_NAME)
        self.duration, self.waveform = duration, waveform

    def to_dict(self, index: int) -> dict:
        return {**super().to_dict(index), "duration_secs": round(self.duration, 2), "waveform": self.waveform}


def voice_shape(path: Path, points: int = 64) -> tuple[float, str] | None:
    """(seconds, base64 waveform of up to 256 one-byte levels) for an audio file, measured with ffmpeg."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        decoded = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", "8000", "-f",
                                  "s16le", "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    samples = memoryview(decoded.stdout).cast("h") if decoded.returncode == 0 and len(decoded.stdout) >= 2 else None
    if not samples:
        return None
    size = max(1, len(samples) // points)
    levels = [max(abs(value) for value in samples[start:start + size]) for start in range(0, len(samples), size)]
    top = max(levels) or 1
    return len(samples) / 8000, base64.b64encode(bytes(min(255, int(255 * level / top))
                                                        for level in levels[:256])).decode()


class SimpleMessage:
    def __init__(self, message_id):
        self.id = message_id


class WhaleChanBot(Interface):
    def __init__(self, name: str, token: str, data_dir: Path, attachment_max_mb: float = 25.0, config=None,
                 persona: str = "whale_chan"):
        self.name, self.token, self.persona = name, token, persona
        self.port = None
        self.attachments = Path(data_dir) / "attachments"
        self.attachment_max = int(attachment_max_mb * 1024 * 1024)
        self.display = Display(markdown=True, edits=True, progress=True, quotes=True, files=True,
                               max_upload_mb=10, max_text_chars=LIMIT)
        self.operations = ()
        config = dict(config or {})
        self.math = MathRenderer(Path(data_dir) / "math", config)
        self.slash_names = tuple(config.get("slash_commands", SLASH_DEFAULT) or ())
        self.interactions = slash.Interactions()
        self.client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._speaking: asyncio.Task | None = None
        self._channels: dict[str, object] = {}
        self._progress: OrderedDict[str, discord.Message] = OrderedDict()
        self._owners: list[str] = []
        speak_up = dict(config.get("speak_up") or {})
        self.call_them = str(speak_up.get("call_them", "主人"))
        self.speak_up = SpeakUp(Settings.of(speak_up), Ledger(Path(data_dir) / "speak_up.json"), self._speak_up_submit,
                                owners=lambda: self._owners, names=lambda user: self.call_them, standing=self._standing)

    def _standing(self, user: str) -> dict:
        """What modules publish about her and this person (contracts 1.9): the companion's plans and mood."""
        facts = getattr(self.port, "facts", None) if self.port is not None else None
        return dict(facts(self.persona, str(user))) if callable(facts) else {}

    async def start(self, port) -> None:
        self.port = port
        intents = discord.Intents.default()
        intents.message_content = True
        self.client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.client.event(self.on_ready)
        self.client.event(self.on_message)
        self.client.event(self.on_interaction)
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
        for task in (self._speaking, self._task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def on_ready(self) -> None:
        try:
            application = await self.client.application_info()
            team = getattr(application, "team", None)
            self._owners = [str(member.id) for member in team.members] if team else [str(application.owner.id)]
            self.port.set_owners(self._owners)
        except discord.HTTPException as error:
            self.port.log("owner_lookup_failed", error=type(error).__name__)
        if self._speaking is None:
            self._speaking = asyncio.create_task(self._speak_up_loop())
        await self.sync_commands()
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
        if text.startswith(MARK):  # only she starts messages with her impulse's mark
            text = text[len(MARK):]
        if direct:
            self.speak_up.ledger.heard(str(message.author.id), message.created_at.timestamp())
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

    # ---- slash commands and buttons ----------------------------------------------------------------
    def commands(self) -> list[Command]:
        catalog = {command.name: command for command in self.port.commands()}
        chosen = [catalog[name] for name in self.slash_names if name in catalog]
        return [*(command for command, _ in LOCAL.values()), *chosen]

    async def sync_commands(self) -> None:
        definitions = slash.payload(self.commands())
        digest = slash.fingerprint(definitions)
        if self.port.bottle.get("slash_fingerprint") == digest:
            return
        try:
            await self.client.http.bulk_upsert_global_commands(self.client.application_id, definitions)
        except Exception as error:
            self.port.log("slash_sync_failed", error=type(error).__name__, detail=str(error)[:200])
            return
        self.port.bottle.set("slash_fingerprint", digest)

    async def on_interaction(self, interaction) -> None:
        try:
            if interaction.type == discord.InteractionType.application_command:
                data = interaction.data or {}
                command = slash.find(self.commands(), data)
                if command is None:
                    await interaction.response.send_message("That command is no longer available.", ephemeral=True)
                    return
                values, _ = slash.values(command, data)
                if command.name == "speakup":
                    await interaction.response.send_message(await self._speak_up_command(
                        str(interaction.user.id), str(values.get("do") or "status")), ephemeral=True)
                    return
                await interaction.response.defer(thinking=True)
                if command.name in LOCAL:
                    text = LOCAL[command.name][1].format(**{option.name: values.get(option.name) or ""
                                                            for option in command.options}).strip()
                else:
                    text = command.render(values)
                await self._submit_interaction(interaction, text, placeholder=True)
            elif (interaction.type == discord.InteractionType.component
                  and str((interaction.data or {}).get("custom_id", "")).startswith(slash.BUTTON_PREFIX)):
                await interaction.response.defer()
                await self._submit_interaction(interaction, interaction.data["custom_id"][len(slash.BUTTON_PREFIX):],
                                               placeholder=False)
        except Exception as error:
            self.port.log("interaction_failed", error=type(error).__name__, detail=str(error)[:200])

    async def _submit_interaction(self, interaction, text: str, placeholder: bool) -> None:
        conversation = f"dm:{interaction.user.id}" if interaction.guild is None else str(interaction.channel_id)
        if interaction.channel is not None and hasattr(interaction.channel, "send"):
            self._channels[conversation] = interaction.channel
        pending = self.interactions.add(str(interaction.id), interaction, conversation, placeholder)
        limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
        result = await self.port.submit(Request(
            id=str(interaction.id), address=Address(self.name, conversation),
            sender=Sender(str(interaction.user.id), interaction.user.display_name), text=text,
            received_at=interaction.created_at.timestamp(), is_direct=interaction.guild is None, mentions_me=True,
            display=dataclasses.replace(self.display, max_upload_mb=round(limit / 1024 ** 2, 2))))
        if not result and pending.placeholder and result.reason not in ("duplicate", "locked"):
            pending.placeholder = False
            await interaction.edit_original_response(content="I can't do that here.")

    # ---- speaking up first ---------------------------------------------------------------------------
    async def _speak_up_loop(self) -> None:
        while True:
            await asyncio.sleep(max(60.0, self.speak_up.settings.every_min * 60))
            try:
                for entry in await self.speak_up.tick():
                    self.port.log("speak_up", **{key: entry[key] for key in ("hit", "line", "reason") if key in entry},
                                  sent=entry.get("sent"), dry_run=entry.get("dry_run", False))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.port.log("speak_up_failed", error=type(error).__name__, detail=str(error)[:200])

    async def _speak_up_submit(self, user: str, name: str, motive: str) -> bool:
        """Her impulse, handed to her engine in their DM. Interfaces only submit requests for people, so it goes
        in their name, marked in `extra` (which only this code sets) and shown to her as her own thought."""
        result = await self.port.submit(Request(
            id=f"speak-up-{user}-{time.time_ns()}", address=Address(self.name, f"dm:{user}"),
            sender=Sender(str(user), name), text=MARK + motive, received_at=time.time(),
            is_direct=True, mentions_me=True, display=self.display, extra={"speak_up": motive}))
        return bool(result)

    async def _speak_up_command(self, user: str, action: str) -> str:
        if user not in self._owners:
            return "Only her owner can change how she speaks up."
        ledger = self.speak_up.ledger
        if action in ("on", "off", "dry"):
            ledger.overrides.update({"on": {"enabled": True, "dry_run": False}, "off": {"enabled": False},
                                     "dry": {"enabled": True, "dry_run": True}}[action])
            ledger.save()
        if action == "now":
            entry = await self.speak_up.now(user)
            return f"🐳 She is messaging you now: {entry.get('motive')}" if entry.get("sent") else \
                f"Could not start it: {entry.get('error') or 'the core refused the request'}"
        state = self.speak_up.rehearse(user)
        mode = "off" if not state["enabled"] else ("dry run (writes down, never sends)" if state["dry_run"] else "on")
        lines = [f"Speak-up: {mode}, every {self.speak_up.settings.every_min:g} min.",
                 f"Hit line now: {state['how']}.", f"Would say because: {state['motive']}"]
        if state["blocked"]:
            lines.insert(1, f"Not now: {state['blocked']}.")
        recent = ledger.person(user).log[-3:]
        if action == "status" and recent:
            lines.append("Last rolls: " + "; ".join(
                f"{time.strftime('%H:%M', time.localtime(item['at']))} {'hit' if item['hit'] else 'miss'} "
                f"({item['reason']})" for item in recent))
        return "\n".join(lines)[:1900]

    def _view(self, reply: Reply):
        if not reply.actions:
            return None
        view = discord.ui.View(timeout=None)
        for action in reply.actions[:5]:
            view.add_item(discord.ui.Button(label=action.label[:80], custom_id=(slash.BUTTON_PREFIX + action.command)[:100]))
        return view

    # ---- outgoing ---------------------------------------------------------------------------------
    async def deliver(self, reply: Reply) -> Delivery:
        if self.client is None or not self.client.is_ready():
            return Delivery(False, detail="not_connected")
        channel = await self._channel(reply.address.conversation)
        if channel is None:
            return Delivery(False, detail="unknown_conversation")
        pending = self.interactions.get(reply.in_reply_to)
        if pending is not None and pending.conversation != reply.address.conversation:
            pending = None

        async def post(reference=None, **options):
            options = {key: value for key, value in options.items() if value is not None}
            if pending is None:
                return await channel.send(reference=reference, **options)
            if pending.placeholder:
                pending.placeholder = False
                if not reply.private:
                    file = options.pop("file", None)
                    return await pending.interaction.edit_original_response(
                        **options, **({"attachments": [file]} if file is not None else {}))
                try:
                    await pending.interaction.delete_original_response()
                except discord.HTTPException:
                    pass
            return await pending.interaction.followup.send(ephemeral=reply.private, wait=True, **options)

        try:
            if reply.kind == "progress":
                if pending is not None and pending.placeholder:
                    message = await pending.interaction.edit_original_response(content=f"-# ⏳ {reply.text}"[:LIMIT])
                    return Delivery(True, message_id=str(message.id))
                return await self._show_progress(channel, reply)
            if reply.run_id and reply.kind in ("text", "file", "notice"):
                await self._clear_progress(reply.run_id)
            reference = (discord.MessageReference(message_id=int(reply.quote), channel_id=channel.id,
                                                  fail_if_not_exists=False)
                         if pending is None and reply.quote and str(reply.quote).isdigit() else None)
            if reply.address.conversation.startswith("dm:") and reply.kind in ("text", "file"):
                self.speak_up.ledger.spoke(reply.address.conversation[3:], time.time())
            if reply.kind == "file" and reply.file.name == VOICE_NAME and pending is None:
                sent = await self._send_voice(channel, Path(reply.file.path))
                if sent is not None:
                    return Delivery(True, message_id=str(sent.id))
            if reply.kind == "file":
                sent = await post(content=reply.text[:LIMIT] or None, reference=reference,
                                  file=discord.File(reply.file.path, filename=reply.file.name or None))
                return Delivery(True, message_id=str(sent.id))
            text = self.render(reply)
            if not text.strip():
                return Delivery(True, detail="suppressed")
            first, parts, view = None, await self._parts(reply, text), self._view(reply)
            for index, (kind, value) in enumerate(parts):
                if kind == "image":
                    try:
                        sent = await post(file=discord.File(value, filename="formula.png"),
                                          reference=reference if first is None else None)
                    finally:
                        Path(value).unlink(missing_ok=True)
                else:
                    sent = await post(content=value, reference=reference if first is None else None,
                                      view=view if index == len(parts) - 1 else None)
                first = first or sent
            return Delivery(True, message_id=str(first.id))
        except (discord.Forbidden, discord.NotFound) as error:
            return Delivery(False, detail=type(error).__name__)
        except discord.HTTPException as error:
            return Delivery(False, detail=f"HTTP {error.status}", uncertain=error.status >= 500)

    async def _send_voice(self, channel, path: Path):
        """Post Ogg Opus as a Discord voice message (the playable bubble with a waveform); None to fall back to
        a plain audio file (no ffmpeg to measure it, or Discord refused)."""
        shape = await asyncio.to_thread(voice_shape, path)
        if shape is None:
            return None
        duration, waveform = shape
        try:
            with discord.http.handle_message_parameters(file=VoiceFile(path, duration, waveform),
                                                        flags=discord.MessageFlags(voice=True)) as params:
                data = await self.client.http.send_message(channel.id, params=params)
            return SimpleMessage(data["id"])
        except discord.HTTPException as error:
            self.port.log("voice_message_failed", status=getattr(error, "status", 0), detail=str(error)[:200])
            return None

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

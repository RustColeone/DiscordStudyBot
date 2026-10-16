"""Discord extras ported from the legacy bot: music, the study clock and math rendering."""

import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from contracts import Address, Reply
from tests.core_support import settle
from tests.test_core_discord import DiscordInterfaceTests, FakeChannel, FakeUser, incoming

HERE = Path(__file__).resolve().parent
PUBLIC_WRITES = ["discord.music", "discord.music_control", "discord.queue_music", "discord.clock_start",
                 "discord.clock_stop"]


class FakeVoiceClient:
    def __init__(self, channel):
        self.channel, self.connected = channel, True
        self.source, self.after, self.paused = None, None, False
        self.played = []

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.source is not None and not self.paused

    def is_paused(self):
        return self.source is not None and self.paused

    def play(self, source, after=None):
        if self.source is not None:
            raise discord.ClientException("Already playing audio.")
        self.source, self.after, self.paused = source, after, False
        self.played.append(source)

    def stop(self):
        after, self.source, self.after, self.paused = self.after, None, None, False
        if after is not None:
            after(None)

    def finish(self):
        self.stop()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    async def disconnect(self, force=False):
        self.connected = False


class FakeVoiceChannel:
    def __init__(self, channel_id, name="Lounge"):
        self.id, self.name = channel_id, name
        self.clients = []

    async def connect(self):
        self.clients.append(FakeVoiceClient(self))
        return self.clients[-1]


class FakeGuild:
    def __init__(self, guild_id=7):
        self.id, self.filesize_limit = guild_id, 25 * 1024 * 1024
        self.members = {}

    def get_member(self, member_id):
        return self.members.get(member_id)

    def join_voice(self, user, channel):
        self.members[user.id] = SimpleNamespace(id=user.id, voice=SimpleNamespace(channel=channel))


class GuildChannel(FakeChannel):
    def __init__(self, channel_id, guild):
        super().__init__(channel_id)
        self.guild = guild


class ExtrasTestBase(unittest.IsolatedAsyncioTestCase):
    start = DiscordInterfaceTests.start
    say = DiscordInterfaceTests.say

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=HERE, prefix=".tmp-discord-extras-")
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def begin(self, discord_config=None, preapprove=True):
        assignment = {"engine": "probe", "personas": ["p"], "preapprove": PUBLIC_WRITES if preapprove else []}
        await self.start({"extension_config": {"discord": discord_config or {}}, "assignments": {"*": assignment}})
        self.server = FakeGuild()
        self.text = GuildChannel(556, self.server)
        self.client.channels[556] = self.text

    async def command(self, text, author=None, channel=None):
        channel = channel or self.text
        guild = getattr(channel, "guild", None)
        mentions = [self.client.user] if guild is not None else []
        await self.say(incoming(channel, author or self.alice, f"<@999> {text}" if guild else text, guild,
                                mentions=mentions))
        return channel.texts()[-1]


class MusicTests(ExtrasTestBase):
    async def begin_music(self, **config):
        music_dir = self.tmp / "music"
        music_dir.mkdir()
        for name in ("song-a.mp3", "song-b.mp3"):
            (music_dir / name).write_bytes(b"x")
        playlist = self.tmp / "musicList.txt"
        playlist.write_text("2\nsong-a.mp3\nsong-b.mp3\n", encoding="utf8")
        await self.begin({"music_dir": str(music_dir), "music_list": str(playlist), **config})
        music = self.discord.music
        music.voice_problem = lambda: None
        music.extract = lambda url: {"title": f"Song {url[-1]}", "url": f"stream:{url}"}
        music.make_source = lambda location, stream: Path(location).name if not stream else location
        self.lounge = FakeVoiceChannel(900)

    async def advance(self):
        await asyncio.sleep(0)
        await asyncio.gather(*self.discord.music._tasks)

    async def test_music_operations_are_public_and_declared_honestly(self):
        await self.begin()
        capabilities = self.core.manager.capabilities
        for name, risk, command in [("discord.music", "write", "music"), ("discord.music_status", "read", None),
                                    ("discord.music_control", "write", None), ("discord.queue_music", "write", None),
                                    ("discord.time", "read", "time"), ("discord.clock_start", "write", "start"),
                                    ("discord.clock_stop", "write", "stop")]:
            capability = capabilities[name]
            self.assertFalse(capability.owner_only, name)
            self.assertEqual((capability.risk.value, capability.command), (risk, command))
        self.assertTrue(capabilities["discord.status"].owner_only)

    async def test_music_plays_the_playlist_in_the_requesters_voice_channel(self):
        await self.begin_music()
        self.assertEqual(await self.command("$music init"), "You need to be in a voice channel first!")
        self.server.join_voice(self.alice, self.lounge)
        self.assertEqual(await self.command("$music init"), "Music player initialized in Lounge")
        voice = self.lounge.clients[0]
        self.assertEqual(voice.played, ["song-b.mp3"])
        self.assertEqual(await self.command("$music -y https://youtu.be/1 --queue"), "➕ Added to queue: **Song 1**")
        self.assertEqual(await self.command("$music --next"),
                         "Music player is now playing the next song: Song 1")
        self.assertEqual(voice.played[-1], "stream:https://youtu.be/1")
        voice.finish()
        await self.advance()
        self.assertEqual(voice.played[-1], "song-a.mp3")
        self.assertEqual(await self.command('$music -y "https://youtu.be/2" https://youtu.be/3'),
                         "🎵 Now playing: **Song 2**\n➕ Added 1 more to queue")
        self.assertEqual(await self.command("$music pause"), "Music player paused")
        self.assertTrue(voice.is_paused())
        self.assertEqual(await self.command("$music play"), "Music player started")
        self.assertTrue(voice.is_playing())
        self.assertEqual(await self.command("$music status"), "Music player is playing in Lounge: #2 Song 2")
        listing = await self.command("$music queue")
        self.assertIn("▶ 2. Song 2", listing)
        self.assertIn("5 songs", listing)
        self.assertEqual(await self.command("$music prev"), "Music player is now playing the previous song: song-a")
        self.assertEqual(await self.command("$music stop"), "Music player stopped")
        self.assertFalse(voice.is_connected())
        await self.advance()
        self.assertEqual(len(voice.played), 5)
        stored = self.discord.port.bottle.get("music:7")
        self.assertEqual((stored["index"], len(stored["tracks"])), (0, 5))
        self.assertEqual(await self.command("$music pause"), "Music player is not initialized. Use `$music init` first")

    async def test_model_operations_drive_the_same_player(self):
        await self.begin_music()
        self.server.join_voice(self.owner, self.lounge)
        await self.command('$call discord.music_control {"action": "initialize"}', author=self.owner)
        await self.command('$call discord.queue_music {"url": "https://youtu.be/9", "play_now": true}',
                           author=self.owner)
        self.assertEqual(self.lounge.clients[0].played[-1], "stream:https://youtu.be/9")
        self.assertEqual(await self.command("$call discord.music_status {}", author=self.owner),
                         "Music player is playing in Lounge: #3 Song 9")

    async def test_music_fails_cleanly_without_voice_support_or_outside_servers(self):
        await self.begin_music()
        self.discord.music.voice_problem = lambda: "The davey library is not installed (pip install davey)."
        self.server.join_voice(self.alice, self.lounge)
        self.assertEqual(await self.command("$music init"),
                         "⚠️ Voice playback is unavailable: The davey library is not installed (pip install davey).")
        self.assertEqual(self.lounge.clients, [])
        dm = FakeChannel(81, private=True)
        self.assertTrue((await self.command("$music status", channel=dm)).startswith("Music works in a server"))
        self.assertTrue((await self.command("$music --bogus")).startswith("❌ Unknown option: --bogus"))
        self.assertIn("$music init", await self.command("$music"))

    async def test_real_voice_check_reports_instead_of_raising(self):
        await self.begin({"ffmpeg": "definitely-not-ffmpeg"})
        problem = self.discord.music.voice_problem()
        self.assertIsInstance(problem, str)

    async def test_interface_stop_disconnects_voice(self):
        await self.begin_music()
        self.server.join_voice(self.alice, self.lounge)
        await self.command("$music init")
        await self.discord.stop()
        self.assertFalse(self.lounge.clients[0].is_connected())


class ClockTests(ExtrasTestBase):
    async def test_time_shows_the_study_clock(self):
        await self.begin()
        text = await self.command("$time")
        self.assertTrue(text.startswith("```"))
        self.assertIn("CN-BJ> ", text)
        self.assertIn("UK-LD> ", text)

    async def test_live_clock_is_edited_until_stopped_and_stops_with_the_interface(self):
        await self.begin()
        module = sys.modules[type(self.discord.clocks).__module__]
        ticks = []
        original = module.clock_text
        module.clock_text = lambda now=None: (ticks.append(1), f"tick {len(ticks)}")[1]
        self.addCleanup(setattr, module, "clock_text", original)
        self.discord.clocks.interval_s = 0.01
        self.assertEqual(await self.command("$start"), "Starting Clock")
        clock = self.text.sent[0]
        await asyncio.sleep(0.1)
        self.assertGreater(len(ticks), 2)
        self.assertEqual(clock.content, f"tick {len(ticks)}")
        self.assertEqual(await self.command("$stop"), "Stopping Timer")
        self.assertEqual(await self.command("$stop"), "No active clock to stop")
        await self.command("$start")
        self.assertTrue(self.discord.clocks.running("556"))
        await self.discord.stop()
        self.assertFalse(self.discord.clocks.running("556"))

    async def test_clock_commands_from_people_need_approval_unless_preapproved(self):
        await self.begin(preapprove=False)
        self.assertTrue((await self.command("$start")).startswith("⏳ Waiting for the owner's approval"))
        self.assertFalse(self.discord.clocks.running("556"))


class MathTests(ExtrasTestBase):
    async def deliver(self, text, **fields):
        return await self.discord.deliver(Reply(Address("discord", "555"), "text", text, **fields))

    async def test_inline_math_becomes_unicode_and_other_dollars_stay(self):
        await self.begin()
        await self.deliver(r"Use $Z_{out} \ll Z_{in}$ and $x^2 \to \infty$.")
        self.assertEqual(self.room.texts()[-1], "Use Zₒᵤₜ ≪ Zᵢₙ and x² → ∞.")
        for text in ["It costs $5 and $10.\n```python\nprice = '$x^2'\n```",
                     "Commands: $approve <id>, $deny <id>, $help", "Run `$x^2$` literally"]:
            await self.deliver(text)
            self.assertEqual(self.room.texts()[-1], text)
        await self.deliver(r"$a+b$ stays", source="owner")
        self.assertEqual(self.room.texts()[-1], "$a+b$ stays")

    async def test_display_math_is_sent_as_images_between_text_parts(self):
        await self.begin()
        delivery = await self.deliver("Before.\n\n$$Z_L = Z_S^*$$\n\nAfter.", speaker="Luna", quote="321")
        sent = self.room.sent
        self.assertTrue(delivery.ok)
        self.assertEqual(delivery.message_id, str(sent[0].id))
        self.assertEqual([message.content for message in sent], ["**Luna**\nBefore.\n\n", None, "\n\nAfter."])
        self.assertEqual(sent[1].file.filename, "formula.png")
        self.assertEqual(sent[0].reference.message_id, 321)
        self.assertEqual([message.reference for message in sent[1:]], [None, None])
        self.assertEqual(list((self.core.config.data_dir / "extensions" / "discord" / "math").iterdir()), [])

    async def test_rendered_formula_is_a_png_on_a_fixed_canvas(self):
        await self.begin({"math_width_px": 800, "math_dpi": 100})
        renderer = self.discord.math
        path = renderer.render(r"\sin\!\left(\tfrac{\pi}{2}-\alpha\right)=\cos\alpha")
        self.addCleanup(path.unlink, missing_ok=True)
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        from PIL import Image
        with Image.open(path) as image:
            self.assertEqual(image.width, 800)

    async def test_failed_or_excess_formulas_fall_back_to_text_and_long_text_still_splits(self):
        await self.begin({"math_max_images": 1})
        await self.deliver("$$x=1$$\n$$y^2=2$$")
        sent = self.room.sent
        self.assertEqual((sent[0].file.filename, sent[1].content), ("formula.png", "```text\ny² = 2\n```"))

        def broken(formula):
            raise ValueError("bad formula")
        self.discord.math.render = broken
        await self.deliver(("word " * 500) + "$$a+b$$")
        tail = [message.content for message in self.room.sent[2:]]
        self.assertTrue(all(len(content) <= 2000 for content in tail))
        self.assertEqual(tail[-1], "```text\na + b\n```")

    async def test_math_can_be_turned_off(self):
        await self.begin({"math": False})
        await self.deliver("$$x=1$$")
        self.assertEqual(self.room.texts(), ["$$x=1$$"])


if __name__ == "__main__":
    unittest.main()

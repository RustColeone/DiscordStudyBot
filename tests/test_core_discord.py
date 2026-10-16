"""Discord interface tests with a fake discord.py client inside a real core."""

import asyncio
import datetime as dt
import itertools
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from contracts import Address, Request, Sender
from core.app import Core
from tests.core_support import FIXTURES, ROOT, FakeAdapter, extension_module, make_config, settle

ids = itertools.count(1000)


class SentMessage:
    def __init__(self, channel, content, file=None, reference=None, allowed_mentions=None):
        self.id = next(ids)
        self.channel, self.content, self.file, self.reference = channel, content, file, reference
        self.allowed_mentions = allowed_mentions
        self.deleted = False

    async def edit(self, content):
        self.content = content

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id, private=False, fail=None):
        self.id = channel_id
        self.type = discord.ChannelType.private if private else discord.ChannelType.text
        self.name = None if private else f"channel-{channel_id}"
        self.sent: list[SentMessage] = []
        self.fail = fail
        self.typed = 0

    async def send(self, content=None, file=None, reference=None, allowed_mentions=None):
        if self.fail is not None:
            raise self.fail
        message = SentMessage(self, content, file, reference, allowed_mentions)
        self.sent.append(message)
        return message

    async def typing(self):
        self.typed += 1

    def texts(self):
        return [message.content for message in self.sent if not message.deleted]


class FakeUser:
    def __init__(self, user_id, name="Someone", bot=False):
        self.id, self.name, self.display_name, self.bot = user_id, name, name, bot
        self.dm_channel = None

    async def create_dm(self):
        self.dm_channel = FakeChannel(next(ids), private=True)
        return self.dm_channel

    def __str__(self):
        return self.name


class FakeClient:
    def __init__(self, owner_id=42):
        self.user = FakeUser(999, "StudyBot", bot=True)
        self.guilds = [SimpleNamespace(name="Home")]
        self.latency = 0.05
        self.events = {}
        self.users = {}
        self.channels = {}
        self.owner_id = owner_id
        self._stop = asyncio.Event()

    def event(self, function):
        self.events[function.__name__] = function
        return function

    async def start(self, token):
        await self._stop.wait()

    async def close(self):
        self._stop.set()

    def is_closed(self):
        return self._stop.is_set()

    async def application_info(self):
        return SimpleNamespace(team=None, owner=SimpleNamespace(id=self.owner_id))

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        return self.users.setdefault(user_id, FakeUser(user_id))

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


class FakeAttachment:
    def __init__(self, filename, content_type, size=10):
        self.filename, self.content_type, self.size = filename, content_type, size

    async def save(self, path):
        Path(path).write_bytes(b"x" * self.size)


def incoming(channel, author, content, guild=None, mentions=(), reference=None, attachments=()):
    return SimpleNamespace(id=next(ids), channel=channel, author=author, content=content, guild=guild,
                           mentions=list(mentions), reference=reference, attachments=list(attachments),
                           created_at=dt.datetime.now(dt.timezone.utc))


class DiscordInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, overrides=None, token="token", ready=True):
        config = make_config(self.tmp, {
            "extensions": [str(ROOT / "extensions" / "discord"), str(FIXTURES / "probe"),
                           str(FIXTURES / "tools"), str(ROOT / "extensions" / "basic")],
            "owners": {}, "control": None, **(overrides or {})})
        self.core = Core(config, provider_adapters={"fake": FakeAdapter()}, tick_seconds=3600)
        await self.core.build()
        self.discord = self.core.gateway.interfaces["discord"]
        self.client = FakeClient()
        self.discord.token, self.discord.client = token, self.client
        await self.core.start()
        if ready and token:
            await self.discord.on_ready()
        extension_module(self.core, "probe").RELEASE = asyncio.Event()
        self.guild = SimpleNamespace(id=7, filesize_limit=25 * 1024 * 1024)
        self.room = FakeChannel(555)
        self.client.channels[555] = self.room
        self.alice = FakeUser(1, "Alice")
        self.owner = FakeUser(42, "Owner")
        self.client.users[42] = self.owner

    async def say(self, message):
        await self.discord.on_message(message)
        await settle(self.core)

    async def test_direct_messages_are_conversations_answered_in_the_dm(self):
        await self.start()
        dm = FakeChannel(77, private=True)
        await self.say(incoming(dm, self.alice, "echo hello"))
        self.assertEqual(dm.texts(), ["hello"])
        self.assertEqual(dm.typed, 1)
        run = self.core.tank.runs(("done",))[0]
        self.assertEqual(run.address, "discord:dm:1")
        self.assertTrue(run.trigger["is_direct"])

    async def test_server_messages_need_a_mention_or_a_reply_to_the_bot(self):
        await self.start()
        await self.say(incoming(self.room, self.alice, "echo ignored", self.guild))
        self.assertEqual(self.room.texts(), [])
        message = incoming(self.room, self.alice, "<@999> echo hi there", self.guild, mentions=[self.client.user])
        await self.say(message)
        self.assertEqual(self.room.texts(), ["hi there"])
        self.assertEqual(self.room.sent[0].reference, None)
        bot_message = SimpleNamespace(author=self.client.user, content="earlier answer")
        reply = incoming(self.room, self.alice, "echo again", self.guild,
                         reference=SimpleNamespace(message_id=12345, resolved=bot_message))
        await self.say(reply)
        self.assertEqual(self.room.texts()[-1], "again")
        trigger = self.core.tank.runs(("done",))[0].trigger
        self.assertEqual((trigger["reply_to"], trigger["quoted_text"], trigger["display"]["max_upload_mb"]),
                         ("12345", "earlier answer", 25.0))

    def test_split_text_never_exceeds_discords_limit(self):
        from core.loader import import_extension
        _, module = import_extension(ROOT / "extensions" / "discord")
        interface = __import__(module.__name__.rsplit(".", 1)[0] + ".interface", fromlist=["split_text"])
        for text in ("a" * 2000 + " " + "b" * 50, "x" * 1999 + "\n\n" + "y" * 10, "word " * 900):
            self.assertTrue(all(len(chunk) <= 2000 for chunk in interface.split_text(text)))

    async def test_talk_prefixes_address_the_bot_like_a_mention(self):
        await self.start({"extension_config": {"discord": {"talk_prefixes": ["$agent", "$chat"]}}})
        await self.say(incoming(self.room, self.alice, "$agent echo hi", self.guild))
        await self.say(incoming(self.room, self.alice, "$agents echo no", self.guild))
        self.assertEqual(self.room.texts(), ["hi"])

    async def test_long_text_is_split_and_only_the_first_part_quotes(self):
        await self.start()
        reply_text = ("word " * 600).strip()
        delivery = await self.discord.deliver(__import__("contracts").Reply(
            Address("discord", "555"), "text", reply_text, quote="321"))
        self.assertTrue(delivery.ok)
        self.assertEqual(len(self.room.sent), 2)
        self.assertTrue(all(len(message.content) <= 2000 for message in self.room.sent))
        self.assertEqual(self.room.sent[0].reference.message_id, 321)
        self.assertIsNone(self.room.sent[1].reference)
        self.assertEqual(delivery.message_id, str(self.room.sent[0].id))

    async def test_application_owner_becomes_owner_and_approvals_go_to_their_dm(self):
        await self.start()
        self.assertTrue(self.core.manager.is_owner(Request("x", Address("discord", "555"), Sender("42"), "", 0)))
        self.assertEqual(self.core.manager.control, Address("discord", "dm:42"))
        dm = FakeChannel(78, private=True)
        await self.say(incoming(dm, self.alice, 'tool tools.write_value {"key": "k", "value": "v"}'))
        approval = self.owner.dm_channel.texts()[0]
        first, second = approval.split("\n")
        match = re.fullmatch(r'🔐 Approval `(\w+)` · `tools\.write_value` `\{"key": "k", "value": "v"\}`', first)
        self.assertIsNotNone(match, first)
        approval_id = match.group(1)
        run_id = self.core.tank.runs(("waiting_approval",))[0].id
        self.assertEqual(second, f"Requested in `discord:dm:1` (run `{run_id}`). "
                                 f"Reply `$approve {approval_id}` or `$deny {approval_id}`.")
        owner_dm = self.owner.dm_channel
        await self.say(incoming(owner_dm, self.owner, f"$approve {approval_id}"))
        self.assertIn("approved: wrote k (execution 1)", dm.texts())

    async def test_configured_owner_wins_over_the_application_owner(self):
        await self.start({"owners": {"discord": ["5"]}, "control": "discord:dm:5"})
        self.assertFalse(self.core.manager.is_owner(Request("x", Address("discord", "555"), Sender("42"), "", 0)))
        self.assertEqual(self.core.manager.control, Address("discord", "dm:5"))

    async def test_progress_is_one_edited_message_removed_when_the_answer_arrives(self):
        await self.start()
        dm = FakeChannel(79, private=True)
        await self.say(incoming(dm, self.alice, 'tool tools.make_file {"name": "a.txt"}'))
        progress = [message for message in dm.sent if (message.content or "").startswith("⏳")]
        self.assertEqual(len(progress), 1)
        self.assertTrue(progress[0].deleted)
        files = [message for message in dm.sent if message.file is not None]
        self.assertEqual(files[0].file.filename, "a.txt")

    async def test_attachments_are_downloaded_only_when_addressed(self):
        await self.start()
        dm = FakeChannel(80, private=True)
        await self.say(incoming(dm, self.alice, "", attachments=[FakeAttachment("photo.png", "image/png")]))
        trigger = self.core.tank.runs(("done",))[0].trigger
        self.assertEqual((trigger["kind"], trigger["text"]), ("image", "[image]"))
        self.assertTrue(Path(trigger["files"][0]["path"]).is_file())
        await self.say(incoming(self.room, self.alice, "look", self.guild,
                                attachments=[FakeAttachment("other.png", "image/png")]))
        self.assertEqual(len(list((self.core.config.data_dir / "extensions" / "discord" / "attachments").iterdir())), 1)

    async def test_delivery_errors_distinguish_certain_and_uncertain_failures(self):
        from contracts import Reply
        await self.start()
        forbidden = FakeChannel(600, fail=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no"))
        broken = FakeChannel(601, fail=discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "x"))
        self.client.channels.update({600: forbidden, 601: broken})
        first = await self.discord.deliver(Reply(Address("discord", "600"), "text", "hi"))
        second = await self.discord.deliver(Reply(Address("discord", "601"), "text", "hi"))
        third = await self.discord.deliver(Reply(Address("discord", "602"), "text", "hi"))
        self.assertEqual((first.ok, first.uncertain), (False, False))
        self.assertEqual((second.ok, second.uncertain), (False, True))
        self.assertEqual(third.detail, "unknown_conversation")

    async def test_owner_reply_to_an_event_is_relayed_and_operations_work(self):
        await self.start()
        delivery = await self.discord.port.notify("someone needs you", origin=Address("discord", "555"))
        owner_dm = self.owner.dm_channel
        event = owner_dm.sent[-1]
        self.assertTrue(event.content.startswith("📨 someone needs you"))
        reply = incoming(owner_dm, self.owner, "on my way",
                         reference=SimpleNamespace(message_id=int(delivery.message_id), resolved=event))
        await self.say(reply)
        self.assertEqual(self.room.texts(), ["on my way"])
        self.assertTrue(owner_dm.texts()[-1].startswith("```") and "sent to discord:555" in owner_dm.texts()[-1])
        await self.say(incoming(owner_dm, self.owner, "$invite"))
        self.assertIn("discord.com/oauth2/authorize?client_id=999", owner_dm.texts()[-1])
        await self.say(incoming(owner_dm, self.owner, "$discord_status"))
        self.assertIn("1 server(s): Home", owner_dm.texts()[-1])

    async def test_unrequested_status_shares_one_rolling_block_in_the_owner_dm(self):
        from contracts import Reply
        await self.start()
        dm = FakeChannel(81, private=True)
        await self.say(incoming(dm, self.alice, "crash"))
        self.assertEqual(dm.texts(), ["⚠️ Sorry, I couldn't finish that."])
        owner = Address("discord", "dm:42")
        await self.discord.deliver(Reply(owner, "progress", "converting video", run_id="r9",
                                         data={"origin_address": "wechat:room"}))
        await self.discord.deliver(Reply(owner, "progress", "uploading", run_id="r9",
                                         data={"origin_address": "wechat:room"}))
        blocks = self.owner.dm_channel.texts()
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].startswith("```\n") and blocks[0].endswith("\n```"))
        self.assertIn("engine exploded", blocks[0])
        self.assertIn("wechat:room · converting video", blocks[0])
        self.assertIn("wechat:room · uploading", blocks[0])
        await self.discord.deliver(Reply(owner, "event", "reading failed", code="wechat_error",
                                         data={"origin_address": "wechat:_interface", "interface": "wechat"}))
        self.assertEqual(len(self.owner.dm_channel.texts()), 1)
        self.assertIn("wechat · reading failed", self.owner.dm_channel.texts()[0])

    async def test_long_approval_arguments_are_shown_in_a_json_block(self):
        from contracts import Reply
        await self.start()
        arguments = '{"text": "' + "x" * 400 + '"}'
        text = self.discord.render(Reply(Address("discord", "dm:42"), "notice", code="approval_request", run_id="r1",
                                         data={"approval_id": "ab12cd", "capability": "basic.remember",
                                               "address": "discord:555", "arguments": arguments}))
        self.assertTrue(text.startswith("🔐 Approval `ab12cd` · `basic.remember`\n```json\n"))
        self.assertTrue(text.endswith("Reply `$approve ab12cd` or `$deny ab12cd`."))

    async def test_reminder_pings_only_the_person_who_set_it_and_marks_late_delivery(self):
        import time
        from contracts import Reply
        await self.start()
        reminder = Reply(Address("discord", "555"), "notice", "drink water @everyone", code="reminder",
                         data={"sender_id": "42", "sender_name": "Ann", "at": str(time.time() - 3600)})
        self.assertTrue((await self.discord.deliver(reminder)).ok)
        message = self.client.channels[555].sent[-1]
        self.assertTrue(message.content.startswith("⏰ <@42> drink water @everyone\n-# due <t:"))
        self.assertEqual([user.id for user in message.allowed_mentions.users], [42])
        self.assertFalse(message.allowed_mentions.everyone)
        await self.discord.deliver(Reply(Address("discord", "555"), "text", "plain"))
        self.assertIsNone(self.client.channels[555].sent[-1].allowed_mentions)

    async def test_speaker_and_label_name_the_persona_talking(self):
        from contracts import Reply
        await self.start()
        room = Address("discord", "555")
        self.assertEqual(self.discord.render(Reply(room, "text", "hi", speaker="Luna")), "**Luna**\nhi")
        self.assertEqual(self.discord.render(Reply(room, "text", "checking", speaker="Luna", label="Work note")),
                         "**Luna · Work note**\nchecking")
        self.assertEqual(self.discord.render(Reply(room, "text", "plain")), "plain")

    async def test_missing_token_reports_not_ready_without_crashing(self):
        await self.start(token="", ready=False)
        self.assertFalse(self.core.gateway.ready()["discord"][0])
        self.assertEqual(self.core.gateway.ready()["discord"][1], "No Discord bot token is configured.")


class TokenTests(unittest.TestCase):
    def test_the_bot_token_is_read_from_discord_bot_token_first(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        from core.loader import import_extension
        extension, _ = import_extension(ROOT / "extensions" / "discord")
        context = SimpleNamespace(config={}, data_dir=Path(tempfile.mkdtemp()), log=lambda *a, **k: None)
        for environment, expected in (({"DISCORD_BOT_TOKEN": "new", "TOKEN": "old"}, "new"),
                                      ({"DISCORD_BOT_TOKEN": "", "TOKEN": "old"}, "old")):
            with patch.dict(os.environ, {"DISCORD_TOKEN": "", **environment}):
                self.assertEqual(extension.setup(context).interfaces[0].token, expected)


class DefaultConfigurationTests(unittest.TestCase):
    def test_fresh_clone_defaults_load_discord_assistant_and_basic(self):
        import os
        from unittest.mock import patch
        from core.config import CoreConfig
        with patch.dict(os.environ, {"DISCORD_OWNER_ID": "", "CREATOR_USER_ID": ""}):
            config = CoreConfig.load(None)
        self.assertEqual([Path(path).name for path in config.extensions],
                         ["discord", "assistant", "basic", "media", "images", "system", "reminders", "web", "room"])
        self.assertEqual(config.profiles["gemini"][:2], [{"provider": "gemini", "model": "gemini-3.8-flash"},
                                                         {"provider": "gemini", "model": "gemini-3.6-flash"}])
        self.assertEqual(len(config.profiles["luna"]), 4)
        self.assertIn("reminders.create", config.assignments["*"].preapprove)
        self.assertIn("discord.music", config.assignments["*"].preapprove)
        self.assertEqual(config.assignments["*"].engine, "assistant")
        self.assertIsNone(config.control)
        with tempfile.TemporaryDirectory() as directory:
            override = Path(directory) / "core.yml"
            override.write_text("data_dir: data\nenv_files: []\npersonas:\n  assistant: {display_name: Pal, prompt: hi}\n"
                                "assignments:\n  'discord:123': {engine: assistant, persona: assistant, enabled: false}\n")
            with patch.dict(os.environ, {"DISCORD_OWNER_ID": "77"}):
                config = CoreConfig.load(override)
        self.assertEqual(config.data_dir, (Path(directory) / "data").resolve())
        self.assertEqual(config.personas["assistant"].display_name, "Pal")
        self.assertIn("*", config.assignments)
        self.assertFalse(config.assignments["discord:123"].enabled)
        self.assertEqual(str(config.control), "discord:dm:77")


if __name__ == "__main__":
    unittest.main()

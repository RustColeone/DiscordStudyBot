"""The Discord translation layer: slash commands, message actions and buttons become `$` text."""

import asyncio
import datetime as dt
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from contracts import Action, Address, Command, Option, Reply
from tests.core_support import ROOT, FakeAdapter, make_config, settle
from tests.test_core_discord import FakeChannel, FakeClient, FakeUser
from core.app import Core
from core.loader import import_extension

ids = itertools.count(9000)


def slash_module():
    _, module = import_extension(ROOT / "extensions" / "discord")
    return __import__(module.__name__.rsplit(".", 1)[0] + ".slash", fromlist=["payload"])


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction

    async def defer(self, thinking=False, ephemeral=False):
        self.interaction.log.append(("defer", thinking))

    async def send_message(self, content, ephemeral=False):
        self.interaction.log.append(("message", content, ephemeral))


class FakeFollowup:
    def __init__(self, interaction):
        self.interaction = interaction

    async def send(self, ephemeral=False, wait=False, **options):
        self.interaction.log.append(("followup", options.get("content"), ephemeral))
        return SimpleNamespace(id=next(ids))


class FakeInteraction:
    def __init__(self, kind, data, user, channel, guild=None):
        self.id = next(ids)
        self.type, self.data, self.user, self.channel, self.guild = kind, data, user, channel, guild
        self.channel_id = channel.id
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.log = []
        self.views = []
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self)

    async def edit_original_response(self, **options):
        self.log.append(("edit", options.get("content"), bool(options.get("attachments"))))
        self.views.append(options.get("view", "unchanged"))
        return SimpleNamespace(id=self.id, channel=self.channel)

    async def delete_original_response(self):
        self.log.append(("delete",))


class PayloadTests(unittest.TestCase):
    def test_commands_become_application_commands_with_required_options_first(self):
        slash = slash_module()
        commands = [Command("clip", "Clip a video", (Option("start"), Option("url", "Link", required=True),
                                                     Option("fit", "Shrink", "boolean", flag="--fit"),
                                                     Option("format", "Format", choices=("mp4", "gif")))),
                    Command("call", "Owner tool", access="creator"),
                    Command("video", "Download", (Option("url", "Link", required=True),), message_action="Download video")]
        definitions = slash.payload(commands)
        clip, call, video, action = definitions
        self.assertEqual([option["name"] for option in clip["options"]], ["url", "start", "fit", "format"])
        self.assertEqual([option["type"] for option in clip["options"]], [3, 3, 5, 3])
        self.assertEqual(clip["options"][3]["choices"], [{"name": "mp4", "value": "mp4"}, {"name": "gif", "value": "gif"}])
        self.assertEqual(call["default_member_permissions"], "0")
        self.assertNotIn("default_member_permissions", clip)
        self.assertEqual((action["type"], action["name"]), (3, "Download video"))
        self.assertEqual(slash.fingerprint(definitions), slash.fingerprint(slash.payload(commands)))

    def test_invocations_become_values_including_message_actions(self):
        slash = slash_module()
        clip = Command("clip", "", (Option("url"), Option("fit", type="boolean", flag="--fit")),
                       template="[-u {url}][ {fit}]")
        values, files = slash.values(clip, {"name": "clip", "options": [
            {"name": "url", "type": 3, "value": "https://x"}, {"name": "fit", "type": 5, "value": True}]})
        self.assertEqual(clip.render(values), "$clip -u https://x --fit")
        video = Command("video", "", (Option("url"),), message_action="Download video")
        values, _ = slash.values(video, {"type": 3, "name": "Download video", "target_id": "5", "resolved": {
            "messages": {"5": {"content": "look at this https://youtu.be/abc !"}}}})
        self.assertEqual(video.render(values), "$video https://youtu.be/abc")


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        config = make_config(tmp, {
            "extensions": [str(ROOT / "extensions" / "discord"), str(Path(__file__).parent / "core_fixtures" / "probe"),
                           str(Path(__file__).parent / "core_fixtures" / "tools"), str(ROOT / "extensions" / "basic")],
            "owners": {}, "control": None})
        self.core = Core(config, provider_adapters={"fake": FakeAdapter()}, tick_seconds=3600)
        await self.core.build()
        self.discord = self.core.gateway.interfaces["discord"]
        self.client = FakeClient()
        self.synced = []

        async def bulk(application_id, definitions):
            self.synced.append(definitions)
        self.client.http = SimpleNamespace(bulk_upsert_global_commands=bulk)
        self.client.application_id = 999
        self.discord.token, self.discord.client = "token", self.client
        await self.core.start()
        await self.discord.on_ready()
        self.alice = FakeUser(1, "Alice")
        self.owner = FakeUser(42, "Owner")
        self.client.users[42] = self.owner

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def invoke(self, name, options=(), user=None, channel=None):
        channel = channel or FakeChannel(70, private=True)
        interaction = FakeInteraction(discord.InteractionType.application_command,
                                      {"type": 1, "name": name, "options": list(options)}, user or self.alice, channel)
        await self.discord.on_interaction(interaction)
        await settle(self.core)
        return interaction

    async def test_the_catalog_is_registered_once_until_it_changes(self):
        self.assertEqual(len(self.synced), 1)
        names = {definition["name"] for definition in self.synced[0]}
        self.assertTrue({"help", "remember", "approve", "routine", "admin"} <= names)
        await self.discord.on_ready()
        self.assertEqual(len(self.synced), 1)

    async def test_a_slash_command_answers_its_interaction_with_the_same_core_result(self):
        interaction = await self.invoke("now", [{"name": "timezone", "type": 3, "value": "UTC"}])
        self.assertEqual(interaction.log[0], ("defer", True))
        self.assertEqual(interaction.log[1][0], "edit")
        self.assertIn("UTC", interaction.log[1][1])
        history = self.core.tank.recent_messages("discord:dm:1")
        self.assertEqual(history[0].text, "$now UTC")

    async def test_private_replies_replace_the_placeholder_with_an_ephemeral_message(self):
        interaction = await self.invoke("help")
        self.assertEqual([entry[0] for entry in interaction.log], ["defer", "delete", "followup"])
        self.assertTrue(interaction.log[2][2])
        self.assertIn("$help", interaction.log[2][1])

    async def test_progress_in_the_placeholder_has_cancel_until_the_answer_replaces_it(self):
        interaction = FakeInteraction(discord.InteractionType.application_command, {}, self.alice,
                                      FakeChannel(71, private=True))
        self.discord.interactions.add("int-1", interaction, "dm:1")
        address = Address("discord", "dm:1")
        cancel = (Action("Cancel", "$cancel run-1", "danger"),)
        await self.discord.deliver(Reply(address, "progress", "working", in_reply_to="int-1", run_id="run-1",
                                         actions=cancel))
        self.assertEqual(interaction.log[-1], ("edit", "⏳ working", False))
        self.assertEqual(interaction.views[-1].children[0].custom_id, "cmd:$cancel run-1")
        await self.discord.deliver(Reply(address, "text", "the answer", in_reply_to="int-1", run_id="run-1"))
        self.assertEqual(interaction.log[-1], ("edit", "the answer", False))
        self.assertIsNone(interaction.views[-1])
        self.assertNotIn(("delete",), interaction.log)
        # The answer replaced the Cancel button, so a reaction on it no longer cancels anything.
        self.assertNotIn(str(interaction.id), self.discord.port.bottle.get("message_actions") or {})

    async def test_cancelling_from_another_slash_command_removes_the_runs_progress(self):
        address = Address("discord", "dm:1")
        cancel = (Action("Cancel", "$cancel run-1", "danger"),)
        await self.discord.deliver(Reply(address, "progress", "working", in_reply_to="msg-1", run_id="run-1",
                                         actions=cancel))
        dm = self.discord._channels["dm:1"]
        progress = dm.sent[0]
        self.assertIn(str(progress.id), self.discord.port.bottle.get("message_actions"))
        other = FakeInteraction(discord.InteractionType.application_command, {}, self.alice, dm)
        self.discord.interactions.add("int-2", other, "dm:1")
        await self.discord.deliver(Reply(address, "text", "Cancelled run run-1.", in_reply_to="int-2",
                                         run_id="run-1"))
        self.assertTrue(progress.deleted)
        self.assertEqual(other.log[-1], ("edit", "Cancelled run run-1.", False))
        self.assertNotIn(str(progress.id), self.discord.port.bottle.get("message_actions"))

    async def test_progress_outlives_its_interaction_through_the_channel(self):
        channel = FakeChannel(72, private=True)
        interaction = FakeInteraction(discord.InteractionType.application_command, {}, self.alice, channel)
        self.discord.interactions.add("int-3", interaction, "dm:1")
        address = Address("discord", "dm:1")
        await self.discord.deliver(Reply(address, "progress", "working", in_reply_to="int-3", run_id="run-3"))
        self.discord.interactions.get("int-3").at -= 3600  # past Discord's 15 minutes
        await self.discord.deliver(Reply(address, "progress", "still working", in_reply_to="int-3", run_id="run-3"))
        await self.discord.deliver(Reply(address, "text", "done", in_reply_to="int-3", run_id="run-3"))
        self.assertEqual(channel.partial_log, [("edit", interaction.id, "⏳ still working"),
                                               ("delete", interaction.id)])
        self.assertEqual(self.discord._channels["dm:1"].texts(), ["done"])

    async def test_buttons_run_their_command_as_whoever_presses_them(self):
        dm = FakeChannel(78, private=True)
        await self.invoke("remember", [{"name": "text", "type": 3, "value": "buy milk"}], channel=dm)
        approval = self.core.tank.approvals("pending")[0]["id"]
        owner_dm = self.owner.dm_channel
        button = owner_dm.sent[-1].view.children[0]
        self.assertEqual(button.custom_id, f"cmd:$approve {approval}")
        press = FakeInteraction(discord.InteractionType.component, {"custom_id": button.custom_id}, self.owner, owner_dm)
        await self.discord.on_interaction(press)
        await settle(self.core)
        self.assertEqual(press.log[0], ("defer", False))
        self.assertIn("Approved", press.log[1][1])
        self.assertEqual(self.core.tank.approvals("pending"), [])

    async def test_refused_invocations_say_why(self):
        self.core.gateway.admin().set_rule("discord:dm:1", {"enabled": False})
        interaction = await self.invoke("now")
        self.assertEqual(interaction.log[-1], ("edit", "I don't answer in this channel.", False))


if __name__ == "__main__":
    unittest.main()

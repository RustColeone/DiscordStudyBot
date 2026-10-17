"""Tests for the example drop-in plugin `plugins/whale_chan`: the chat engine and its setup."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

PLUGIN = ROOT / "plugins" / "whale_chan"


def step(message, status="complete", actions=()):
    return {"message": message, "status": status, "actions": [{"tool": t, "arguments": a} for t, a in actions]}


class ChatEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None
        environment = patch.dict(os.environ, {"DISCORD_BOT_TOKEN_WHALE_CHAN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, responses, options=None):
        self.adapter = FakeAdapter(responses)
        extensions = [*make_config(self.tmp).raw["extensions"], str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "profiles": {"whale_chan": [{"provider": "fake", "model": "pro"}]},
            "assignments": {"*": {"engine": "chat", "personas": ["whale_chan"], "options": options or {}}}},
            self.adapter)
        return iface(self.core)

    def texts(self, chat, kind="text"):
        return [reply.text for reply in chat.delivered if reply.kind == kind]

    def prompt(self, index):
        return "\n".join(message["content"] for message in self.adapter.calls[index]["messages"])

    async def test_answers_after_working_with_tools_and_posts_only_the_answer(self):
        chat = await self.start([step("Checking the time.", "continue", [("basic.now", {"timezone": "UTC"})]),
                                 step("## Time\nIt is noon (UTC).")])
        await chat.say("what time is it?")
        await settle(self.core)
        self.assertEqual(self.texts(chat), ["## Time\nIt is noon (UTC)."])
        self.assertIn("Checking the time.", self.texts(chat, "progress"))
        self.assertEqual([call["effort"] for call in self.adapter.calls], ["high", "high"])
        self.assertIn("[tool basic.now → success]", self.prompt(1))
        self.assertIn("本鲸", self.adapter.calls[0]["messages"][0]["content"])

    async def test_the_conversation_is_one_thread_until_new(self):
        chat = await self.start([step("Hi 主人!"), step("You said hello."), step("Fresh start, what's up?")])
        for text in ("hello", "what did I say?", "/new", "what did I say?"):
            await chat.say(text)
            await settle(self.core)
        self.assertEqual(self.texts(chat), ["Hi 主人!", "You said hello.", "🐳 好的主人，新的对话开始啦～",
                                            "Fresh start, what's up?"])
        self.assertIn("hello", self.prompt(1))
        self.assertIn("Hi 主人!", self.prompt(1))
        self.assertNotIn("hello", self.prompt(2))
        self.assertNotIn("/new", self.prompt(2))
        self.assertEqual(len(self.adapter.calls), 3)

    async def test_a_long_thread_is_trimmed_to_the_character_budget_newest_first(self):
        chat = await self.start([step("ok")], {"history_limit": 100})
        engine = self.core.manager.engines["chat"]
        engine.history_chars = 50
        from contracts import Address, Sender
        now = time.time()
        for index in range(5):
            await chat.port.observe(Address("chat", "room"), f"message number {index} " + "x" * 10,
                                    sender=Sender("alice", "Alice"), message_id=f"m{index}", at=now - 50 + index)
        await chat.say("latest")
        await settle(self.core)
        prompt = self.prompt(0)
        self.assertIn("message number 4", prompt)
        self.assertNotIn("message number 1", prompt)

    async def test_step_limit_forces_an_answer(self):
        chat = await self.start([step("a", "continue", [("basic.now", {})]),
                                 step("b", "continue", [("basic.now", {"timezone": "UTC"})]), step("Best so far.")],
                                {"max_steps": 2})
        await chat.say("dig deep")
        await settle(self.core)
        self.assertEqual(self.texts(chat), ["Best so far."])
        self.assertIn("used all your steps", self.adapter.calls[-1]["messages"][-1]["content"])


class WhaleChanSetupTests(unittest.TestCase):
    def setup_with(self, config, environment):
        from core.loader import import_extension
        extension, _ = import_extension(PLUGIN)
        context = SimpleNamespace(config=config, data_dir=Path(tempfile.mkdtemp()), log=lambda *a, **k: None)
        with patch.dict(os.environ, environment):
            return extension.setup(context)

    def test_without_a_token_only_the_engine_persona_and_profile_load(self):
        contributions = self.setup_with({}, {"DISCORD_BOT_TOKEN_WHALE_CHAN": ""})
        self.assertEqual(([e.spec.name for e in contributions.engines], [p.name for p in contributions.personas],
                          list(contributions.interfaces)), (["chat"], ["whale_chan"], []))
        self.assertEqual([(c["model"], c.get("vision", True)) for c in contributions.profiles["whale_chan"]],
                         [("deepseek-flash", True)])

    def test_the_token_comes_from_the_environment_or_the_local_config(self):
        from_env = self.setup_with({}, {"DISCORD_BOT_TOKEN_WHALE_CHAN": "secret"})
        self.assertEqual([interface.name for interface in from_env.interfaces], ["whale_chan"])
        self.assertEqual(from_env.interfaces[0].token, "secret")
        self.assertEqual(from_env.assignments["whale_chan:*"]["engine"], "chat")
        self.assertTrue(from_env.admission["whale_chan:*"]["require_mention_in_groups"])
        from_config = self.setup_with({"token": "local", "interface": "whale", "assignment": {"deny": ["system.*"]}},
                                      {"DISCORD_BOT_TOKEN_WHALE_CHAN": ""})
        self.assertEqual(from_config.interfaces[0].token, "local")
        self.assertEqual(from_config.assignments["whale:*"]["deny"], ["system.*"])

    def test_long_text_splits_within_discords_limit(self):
        from core.loader import import_extension
        _, module = import_extension(PLUGIN)
        bot = __import__(module.__name__.rsplit(".", 1)[0] + ".bot", fromlist=["split_text"])
        for text in ("a" * 2000 + " " + "b" * 50, "x" * 1999 + "\n\n" + "y" * 10, "word " * 900):
            self.assertTrue(all(len(chunk) <= 2000 for chunk in bot.split_text(text)))

    def test_the_folder_keeps_its_config_out_of_git_on_its_own(self):
        self.assertIn("config.yml", (PLUGIN / ".gitignore").read_text().split())


if __name__ == "__main__":
    unittest.main()


class WhaleSlashTests(unittest.IsolatedAsyncioTestCase):
    async def test_her_commands_are_new_plus_a_short_catalog_and_new_becomes_a_message(self):
        import discord
        from contracts import Command, Delivery, Option, Reply, Address
        from tests.test_core_discord_slash import FakeChannel, FakeInteraction
        from tests.test_core_discord import FakeUser
        from core.loader import import_extension
        _, module = import_extension(PLUGIN)
        bot_module = __import__(module.__name__.rsplit(".", 1)[0] + ".bot", fromlist=["WhaleChanBot"])
        bot = bot_module.WhaleChanBot("whale_chan", "token", Path(tempfile.mkdtemp()))
        submitted = []

        async def submit(request):
            submitted.append(request)
            return SimpleNamespace(__bool__=lambda self: True, reason="accepted")
        bot.port = SimpleNamespace(commands=lambda: [Command("help", "Help"), Command("call", "Owner", access="creator"),
                                                     Command("remind", "Remind", (Option("when"), Option("text")))],
                                   submit=submit, log=lambda *a, **k: None, bottle={})
        self.assertEqual([command.name for command in bot.commands()], ["new", "help", "remind"])
        channel = FakeChannel(70, private=True)
        interaction = FakeInteraction(discord.InteractionType.application_command, {"type": 1, "name": "new"},
                                      FakeUser(1, "Alice"), channel)
        await bot.on_interaction(interaction)
        self.assertEqual(submitted[0].text, "/new")
        bot.client = SimpleNamespace(is_ready=lambda: True)
        delivery = await bot.deliver(Reply(Address("whale_chan", "dm:1"), "text", "🐳 新的对话", in_reply_to=submitted[0].id))
        self.assertTrue(delivery.ok)
        self.assertEqual(interaction.log[-1], ("edit", "🐳 新的对话", False))

"""Tests for the companion module (extensions/companion): aliases and roles in every engine's prompt, by persona."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_core_room as room
from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

NAMES = ("luna", "gemini", "claude", "deepseek")


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, script, settings=None):
        extensions = [*make_config(self.tmp).raw["modules"]["paths"],
                      *(str(ROOT / "extensions" / name) for name in ("companion", "room", "students"))]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "extension_config": {"companion": settings or {}},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in (*NAMES, "grok")},
            "assignments": {"*": {"engine": "room", "personas": list(NAMES)}}}, FakeAdapter(handler=script))
        return iface(self.core)

    async def say(self, chat, text):
        await chat.say(text)
        await settle(self.core)
        return chat.texts("room")[-1]

    @staticmethod
    def seen(script, speaker):
        return "\n".join(message["content"] for message in script.prompts[speaker][-1])

    async def test_a_name_of_yours_reaches_her_and_only_her(self):
        script = room.Script(Luna=[room.turn("Hi!"), room.turn("Yes?")], Gemini=[room.turn("Hey.")])
        chat = await self.start(script)
        self.assertIn("nobody yet", await self.say(chat, "$alias luna Lulu"))  # not met yet
        await self.say(chat, "hello")  # Luna answers, so the companion knows her name
        self.assertEqual(await self.say(chat, "$alias luna Lulu"), "You call Luna Lulu now; she will know.")
        await self.say(chat, "Luna, are you there?")
        await self.say(chat, "Gemini, hi")
        self.assertIn("(Alice calls you Lulu.)", self.seen(script, "Luna"))
        self.assertNotIn("Lulu", self.seen(script, "Gemini"))
        self.assertEqual(await self.say(chat, "$alias"), "What you call them:\nLuna: Lulu")
        self.assertEqual(await self.say(chat, "$alias lulu Lu"), "You call Luna Lu now; she will know.")  # by alias
        self.assertEqual(await self.say(chat, "$alias Luna"), "You call Luna Lu.")
        self.assertIn("Luna: no role yet; you call her Lu", await self.say(chat, "$bond"))
        self.assertEqual(await self.say(chat, "$alias luna reset"), "Luna is Luna to you again.")
        self.assertIn("No student called 'nobody'", await self.say(chat, "$alias nobody X"))

    async def test_roles_from_the_settings_and_one_person_across_bots(self):
        script = room.Script(Luna=[room.turn("Hi!")])
        chat = await self.start(script, {"platforms": {"chat": "discord"}, "language": "zh",
                                         "roles": {"discord:alice": "creator"}})
        await self.say(chat, "hello")
        self.assertIn("（Alice是创造你的人，你们非常亲近。）", self.seen(script, "Luna"))
        self.assertIn("Luna: creator, closeness 100/100", await self.say(chat, "$bond"))
        self.assertTrue((self.tmp / "data" / "extensions" / "companion" / "luna.db").is_file())

    async def test_without_mneme_llm_it_adds_nothing(self):
        from core.loader import import_extension
        _, module = import_extension(ROOT / "extensions" / "companion")
        logged = []
        context = SimpleNamespace(config={}, data_dir=self.tmp, log=lambda event, **fields: logged.append(event))
        saved, module.mneme_llm = module.mneme_llm, None
        try:
            contributions = module.setup(context)
        finally:
            module.mneme_llm = saved
        self.assertEqual((contributions.prompt_parts, contributions.capabilities), ((), ()))
        self.assertEqual(logged, ["mneme_llm_missing"])


if __name__ == "__main__":
    unittest.main()

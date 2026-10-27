"""Tests for the companion module (extensions/companion): what every student keeps of each person, by persona."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_core_room as room
from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

NAMES = ("gpt", "gemini", "claude", "deepseek")


class Mind:
    """The students' scripted turns, and mneme-llm's own calls: its judge, its note writer and its impression."""

    def __init__(self, script, worth=0.9):
        self.script, self.worth = script, worth
        self.judged = self.written = 0

    def __call__(self, messages, schema):
        system = messages[0]["content"]
        if system.startswith("You judge a piece of text"):
            self.judged += 1
            return {"mood_change": {"level": "2", "confidence": 0.9}, "feeling": {"choice": "happy", "confidence": 0.9},
                    "worth_remembering": {"probability": self.worth}}
        if system.startswith("From the exchange below"):
            self.written += 1
            return [{"text": "Alice loves oolong tea.", "kind": "preference", "key": "drink.tea", "importance": 3}]
        if system.startswith("Write a short impression"):
            return "Alice is warm and curious."
        return self.script(messages, schema)


class CompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, handler, settings=None):
        extensions = [*make_config(self.tmp).raw["modules"]["paths"],
                      *(str(ROOT / "extensions" / name) for name in ("companion", "room", "students"))]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "extension_config": {"companion": settings or {}},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in (*NAMES, "grok")},
            "assignments": {"*": {"engine": "room", "personas": list(NAMES)}}}, FakeAdapter(handler=handler))
        return iface(self.core)

    async def say(self, chat, text):
        await chat.say(text)
        await settle(self.core)
        await self.core.manager.idle()  # and what the watchers do after
        return chat.texts("room")[-1]

    @staticmethod
    def seen(script, speaker):
        return "\n".join(message["content"] for message in script.prompts[speaker][-1])

    def facts(self, persona):
        return {name: value for name, value in iface(self.core).port.facts(persona, "alice").items()
                if name.startswith("companion.")}

    async def test_a_name_of_yours_reaches_her_and_only_her(self):
        script = room.Script(GPT=[room.turn("Hi!"), room.turn("Yes?")], Gemini=[room.turn("Hey.")])
        chat = await self.start(script, {"learn": False})
        self.assertIn("nobody yet", await self.say(chat, "$alias luna Lulu"))  # not met yet
        await self.say(chat, "hello")  # GPT answers, so the companion knows her names
        self.assertEqual(await self.say(chat, "$alias luna Lulu"), "You call GPT Lulu now; she will know.")  # (formerly)
        self.assertEqual(self.facts("gpt"), {"companion.alias": "Lulu"})
        await self.say(chat, "GPT, are you there?")
        await self.say(chat, "Gemini, hi")
        self.assertIn("(Alice calls you Lulu.)", self.seen(script, "GPT"))
        self.assertNotIn("Lulu", self.seen(script, "Gemini"))
        self.assertEqual(await self.say(chat, "$alias"), "What you call them:\nGPT: Lulu")
        self.assertEqual(await self.say(chat, "$alias lulu Lu"), "You call GPT Lu now; she will know.")  # by alias
        self.assertEqual(await self.say(chat, "$alias GPT"), "You call GPT Lu.")
        self.assertIn("GPT: no role yet; you call her Lu", await self.say(chat, "$bond"))
        self.assertEqual(await self.say(chat, "$alias gpt reset"), "GPT is GPT to you again.")
        self.assertEqual(self.facts("gpt"), {})
        self.assertIn("No student called 'nobody'", await self.say(chat, "$alias nobody X"))

    async def test_roles_from_the_settings_and_one_person_across_bots(self):
        script = room.Script(GPT=[room.turn("Hi!")])
        chat = await self.start(script, {"platforms": {"chat": "discord"}, "language": "zh", "learn": False,
                                         "roles": {"discord:alice": "creator"}})
        await self.say(chat, "hello")
        self.assertIn("（Alice是创造你的人，你们非常亲近。）", self.seen(script, "GPT"))
        self.assertIn("GPT: creator, closeness 100/100", await self.say(chat, "$bond"))
        self.assertTrue((self.tmp / "data" / "extensions" / "companion" / "gpt.db").is_file())

    async def test_she_learns_from_what_is_said_and_the_pages_see_where_she_stands(self):
        script = room.Script(GPT=[room.turn("Oolong, noted."), room.turn("Oolong tea!")], Gemini=[room.turn("Hm.")])
        mind = Mind(script)
        chat = await self.start(mind, {"language": "en"})
        await self.say(chat, "GPT, I love oolong tea")
        self.assertEqual((mind.judged, mind.written), (1, 1))  # one decision, then the note writer
        facts = self.facts("gpt")
        self.assertEqual(facts["companion.bond"], {"role": "acquaintance", "closeness": 20})  # someone new
        self.assertGreater(facts["companion.mood"]["value"], 70)
        self.assertEqual(facts["companion.mood"]["feelings"][0][0], "happy")
        await self.say(chat, "Luna, which tea do I like?")
        turn = self.seen(script, "GPT")
        self.assertIn("Alice loves oolong tea.", turn)  # her note, for this message (found by its words)
        self.assertIn("(Alice is someone you know; you are not close yet.)", turn)  # an acquaintance
        await self.say(chat, "Gemini, which tea do I like?")
        self.assertNotIn("Alice loves oolong tea.", self.seen(script, "Gemini"))  # GPT's note is hers alone
        self.assertIn("GPT: acquaintance, closeness 20/100, mood", await self.say(chat, "$bond"))
        self.assertEqual(self.facts("gemini")["companion.bond"]["role"], "acquaintance")  # she talked with Alice too

    async def test_it_speaks_and_learns_only_in_the_engines_it_is_told_and_can_be_turned_off(self):
        script = room.Script(GPT=[room.turn("Hi!")])
        mind = Mind(script)
        chat = await self.start(mind, {"engines": ["assistant"], "personas": ["gpt"]})
        await self.say(chat, "$alias gpt Lulu")
        await self.say(chat, "GPT, hello")
        self.assertEqual(mind.judged, 0)  # the room is not one of its engines here
        self.assertNotIn("Lulu", self.seen(script, "GPT"))
        self.assertEqual(self.facts("gpt"), {"companion.alias": "Lulu"})
        await self.core.stop()
        self.core = None
        from core.loader import import_extension
        _, module = import_extension(ROOT / "extensions" / "companion")
        context = SimpleNamespace(config={"learn": False}, data_dir=self.tmp / "off", log=lambda *a, **k: None)
        self.assertEqual(module.setup(context).watchers, [])

    async def test_each_student_may_have_settings_of_her_own(self):
        script = room.Script(GPT=[room.turn("Noted.")], Gemini=[room.turn("Noted too.")])
        mind = Mind(script)
        chat = await self.start(mind, {"students": {"gemini": {"learn": False, "language": "zh"},
                                                    "gpt": {"decisions": "nonsense", "memory_language": "English"}}})
        await self.say(chat, "GPT, I love oolong tea")
        await self.say(chat, "Gemini, I love oolong tea")
        self.assertEqual(mind.judged, 1)  # GPT learns; Gemini does not
        self.assertNotIn("companion.bond", self.facts("gemini"))
        from core.loader import import_extension
        _, module = import_extension(ROOT / "extensions" / "companion")
        settings = module.Settings({"decisions": "LUNA", "config": "whale.toml",
                                    "students": {"gpt": {"call_owner": "主人", "personas": ["x"]}}}, self.tmp)
        self.assertEqual((settings.decisions, settings.config_file), ("luna", str(self.tmp / "whale.toml")))
        self.assertEqual((settings.of("gpt").call_owner, settings.of("gpt").personas, settings.of("gemini").call_owner),
                         ("主人", (), ""))  # (`personas` is not a student's own setting)

    async def test_the_pages_see_what_her_file_already_holds_at_start(self):
        import mneme_llm
        folder = self.tmp / "data" / "extensions" / "companion"
        folder.mkdir(parents=True)
        with mneme_llm.Mneme(str(folder / "gpt.db"), "gpt", luna=False, micro=False, embed=False,
                             meaning_micro=False, memory_language=None) as memory:
            memory.alias("chat:alice", "Lulu")
            memory.relate("chat:alice", "friend")
        await self.start(room.Script())
        self.assertEqual(self.facts("gpt")["companion.alias"], "Lulu")
        self.assertEqual(self.facts("gpt")["companion.bond"], {"role": "friend", "closeness": 50})

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
        self.assertEqual((contributions.prompt_parts, contributions.capabilities, contributions.watchers),
                         ((), (), ()))
        self.assertEqual(logged, ["mneme_llm_missing"])


if __name__ == "__main__":
    unittest.main()

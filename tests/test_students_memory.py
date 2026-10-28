"""Tests for the students' memory (extensions/students/memory.py, the companion module until 2026-10-27): what
every student keeps of each person. Agents keep nothing."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests import test_core_room as room
from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

NAMES = ("gpt", "gemini", "claude", "deepseek")


def memory_module():
    import importlib
    from core.loader import import_extension
    return importlib.import_module(import_extension(ROOT / "extensions" / "students")[1].__package__ + ".memory")


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

    async def start(self, handler, settings=None, assignments=None):
        extensions = [*make_config(self.tmp).raw["modules"]["paths"],
                      *(str(ROOT / "extensions" / name) for name in ("room", "students"))]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "extension_config": {"students": {"learn": True, **(settings or {})}},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in (*NAMES, "grok")},
            "assignments": {**(assignments or {}), "*": {"engine": "room", "personas": list(NAMES)}}},
            FakeAdapter(handler=handler))
        return iface(self.core)

    async def say(self, chat, text, conversation="room", sender="alice"):
        await chat.say(text, conversation=conversation, sender=sender)
        await settle(self.core)
        await self.core.manager.idle()  # and what the watchers do after
        return chat.texts(conversation)[-1]

    @staticmethod
    def seen(script, speaker):
        return "\n".join(message["content"] for message in script.prompts[speaker][-1])

    def facts(self, persona, sender="alice"):
        return {name: value for name, value in iface(self.core).port.facts(persona, sender).items()
                if name.startswith("students.")}

    async def test_a_name_of_yours_reaches_her_and_only_her(self):
        script = room.Script(GPT=[room.turn("Hi!"), room.turn("Yes?")], Gemini=[room.turn("Hey.")])
        chat = await self.start(script, {"learn": False})
        self.assertEqual(await self.say(chat, "$alias luna Lulu"), "You call GPT Lulu now; she will know.")  # her alias
        self.assertEqual(self.facts("gpt"), {"students.alias": "Lulu"})
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
        self.assertTrue((self.tmp / "data" / "extensions" / "students" / "gpt.db").is_file())

    async def test_she_learns_from_what_is_said_and_the_pages_see_where_she_stands(self):
        script = room.Script(GPT=[room.turn("Oolong, noted."), room.turn("Oolong tea!")], Gemini=[room.turn("Hm.")])
        mind = Mind(script)
        chat = await self.start(mind, {"language": "en"})
        await self.say(chat, "GPT, I love oolong tea")
        self.assertEqual((mind.judged, mind.written), (1, 1))  # one decision, then the note writer
        facts = self.facts("gpt")
        self.assertEqual(facts["students.bond"], {"role": "acquaintance", "closeness": 20})  # someone new
        self.assertGreater(facts["students.mood"]["value"], 70)
        self.assertEqual(facts["students.mood"]["feelings"][0][0], "happy")
        await self.say(chat, "Luna, which tea do I like?")
        turn = self.seen(script, "GPT")
        self.assertIn("Alice loves oolong tea.", turn)  # her note, for this message (found by its words)
        self.assertIn("(Alice is someone you know; you are not close yet.)", turn)  # an acquaintance
        await self.say(chat, "Gemini, which tea do I like?")
        self.assertIn("Alice loves oolong tea.", self.seen(script, "Gemini"))  # a group chat: she heard it too
        self.assertIn("GPT: acquaintance, closeness 20/100, mood", await self.say(chat, "$bond"))
        self.assertEqual(self.facts("gemini")["students.bond"]["role"], "acquaintance")  # she talked with Alice too
        self.assertTrue((self.tmp / "data" / "extensions" / "students" / "claude.db").is_file())

    async def test_a_direct_chat_stays_between_the_two(self):
        script = room.Script(GPT=[room.turn("Oolong, noted.")], Gemini=[room.turn("Hm.")])
        chat = await self.start(Mind(script), {"language": "en"},
                                {"chat:dm": {"engine": "room", "personas": ["gpt"]}})
        await self.say(chat, "GPT, I love oolong tea", conversation="dm")
        folder = self.tmp / "data" / "extensions" / "students"
        self.assertEqual(sorted(path.name for path in folder.glob("*.db")), ["gpt.db"])  # nobody else heard it
        await self.say(chat, "Gemini, which tea do I like?")
        self.assertNotIn("oolong", self.seen(script, "Gemini"))

    async def test_one_person_on_several_accounts_is_one_person_to_her(self):
        script = room.Script(GPT=[room.turn("Oolong, noted."), room.turn("Oolong tea!")])
        chat = await self.start(Mind(script), {"language": "en",
                                               "people": {"Ally": ["chat:alice", "chat:al"]}})
        await self.say(chat, "GPT, I love oolong tea")
        await self.say(chat, "GPT, which tea do I like?", sender="al")
        turn = self.seen(script, "GPT")
        self.assertIn("Notes about Ally", turn)  # one name for her, whichever account
        self.assertIn("Alice loves oolong tea.", turn)  # kept from the first account (the note writer's words)
        self.assertIn("(Ally is someone you know", turn)  # her role on the first account
        self.assertEqual(self.facts("gpt", "al")["students.bond"], self.facts("gpt")["students.bond"])

    async def test_it_speaks_and_learns_only_in_the_engines_it_is_told_and_can_be_turned_off(self):
        script = room.Script(GPT=[room.turn("Hi!")])
        mind = Mind(script)
        chat = await self.start(mind, {"engines": ["chimera"], "personas": ["gpt"]})
        await self.say(chat, "$alias gpt Lulu")
        await self.say(chat, "GPT, hello")
        self.assertEqual(mind.judged, 0)  # MomoChat (the room, here) is not one of its engines here
        self.assertNotIn("Lulu", self.seen(script, "GPT"))
        self.assertEqual(self.facts("gpt"), {"students.alias": "Lulu"})
        await self.core.stop()
        self.core = None
        from core.loader import import_extension
        memory = memory_module()
        context = SimpleNamespace(config={"learn": False}, data_dir=self.tmp / "off", log=lambda *a, **k: None)
        self.assertEqual(memory.contributions(context, {"gpt": {"display_name": "GPT"}})["watchers"], [])

    async def test_each_student_may_have_settings_of_her_own(self):
        script = room.Script(GPT=[room.turn("Noted.")], Gemini=[room.turn("Noted too.")])
        mind = Mind(script)
        chat = await self.start(mind, {"students": {"gemini": {"learn": False, "language": "zh"},
                                                    "gpt": {"decisions": "nonsense", "memory_language": "English"}}})
        await self.say(chat, "GPT, I love oolong tea")
        await self.say(chat, "Gemini, I love oolong tea")
        self.assertEqual(mind.judged, 1)  # GPT learns; Gemini does not
        self.assertNotIn("students.bond", self.facts("gemini"))
        from core.loader import import_extension
        module = memory_module()
        settings = module.Settings({"decisions": "LUNA", "config": "whale.toml",
                                    "students": {"gpt": {"call_owner": "主人", "personas": ["x"]}}}, self.tmp)
        self.assertEqual((settings.decisions, settings.config_file), ("luna", str(self.tmp / "whale.toml")))
        self.assertEqual((settings.of("gpt").call_owner, settings.of("gpt").personas, settings.of("gemini").call_owner),
                         ("主人", (), ""))  # (`personas` is not a student's own setting)

    async def test_the_pages_see_what_her_file_already_holds_at_start(self):
        import mneme_llm
        folder = self.tmp / "data" / "extensions" / "students"
        folder.mkdir(parents=True)
        with mneme_llm.Mneme(str(folder / "gpt.db"), "gpt", luna=False, micro=False, embed=False,
                             meaning_micro=False, memory_language=None) as memory:
            memory.alias("chat:alice", "Lulu")
            memory.relate("chat:alice", "friend")
        await self.start(room.Script())
        self.assertEqual(self.facts("gpt")["students.alias"], "Lulu")
        self.assertEqual(self.facts("gpt")["students.bond"], {"role": "friend", "closeness": 50})

    async def test_agents_keep_nothing_and_without_mneme_llm_there_are_no_students(self):
        script = room.Script(Pat=[room.turn("Hi, I'm Pat.")])
        mind = Mind(script)
        chat = await self.start(mind, {"language": "en"}, {"chat:pat": {"engine": "room", "personas": ["p"]}})
        await self.say(chat, "I love oolong tea", conversation="pat")
        self.assertEqual(mind.judged, 0)  # Pat is an agent (core.yml's): no memory
        self.assertNotIn("Notes about", self.seen(script, "Pat"))
        self.assertIn("No student called 'pat'", await self.say(chat, "$alias pat Patty"))
        from core.loader import import_extension
        extension, _ = import_extension(ROOT / "extensions" / "students")
        self.assertEqual((extension.requires, extension.formerly), (("package:mneme-llm>=0.2",), ("companion",)))


if __name__ == "__main__":
    unittest.main()

"""Prompt parts: core.yml's `personas: <name>: parts:`, modules' parts, their rendering and where engines put them."""

import tempfile
import textwrap
import time
import unittest
import warnings
from pathlib import Path

from contracts import Address, Engine, EngineSpec, PartContext, Persona, PromptPart, Request, RunYield, Sender
from core.config import ConfigError, CoreConfig
from core.manager.secrets import Secrets
from core.prompts import PLACE_LIMIT, FileText, parts_for, render, with_parts
from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core, turn_context
from tests.test_core_room import NAMES, Script
from tests.test_core_room import turn as room_turn

ASSISTANT = {"assignments": {"*": {"engine": "assistant", "personas": ["p"]}}}

# A module that gives its own persona parts and adds parts for others (Contributions.prompt_parts).
MODULE = '''
from contracts import Contributions, Extension, Persona, PromptPart


def setup(context):
    own = (PromptPart("style", "Ivy's own style."), PromptPart("habit", "Ivy hums."))
    return Contributions(personas=[Persona("ivy", "Ivy", "You are Ivy.", parts=own)],
                         prompt_parts=[PromptPart("house", lambda c: f"House rules for {c.persona.display_name}."),
                                       PromptPart("only_pat", "Only for Pat.", personas="p")])


EXTENSION = Extension("partsmod", setup, contracts="1")
'''


def reply(message="ok"):
    return {"message": message, "status": "complete", "actions": []}


def context(persona=None, owner=False):
    persona = persona or Persona("p", "Pat", "You are Pat.")
    trigger = Request(id="r", address=Address("chat", "room"), sender=Sender("alice", "Alice"), text="hi",
                      received_at=time.time())
    return PartContext(persona, trigger.address, trigger, owner)


def audited(core):
    return [event for event in core.audit.events if event["event"] == "prompt_part_problem"]


class Temporary(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)

    def config(self, personas):
        return make_config(self.tmp, {"personas": personas})


class ConfigTests(Temporary):
    def test_text_and_file_parts_with_their_defaults(self):
        (self.tmp / "pat.md").write_text("Warm and brief.", encoding="utf-8")
        config = self.config({"p": {"display_name": "Pat", "prompt": "You are Pat.", "parts": [
            {"text": "Be kind."}, {"file": "pat.md", "place": "turn", "order": 5}, {"name": "rules", "text": "x"}]}})
        self.assertEqual(config.personas["p"].parts, (
            PromptPart("part 1", "Be kind."), PromptPart("pat", FileText(self.tmp / "pat.md"), "turn", 5),
            PromptPart("rules", "x")))
        self.assertEqual(config.persona_parts, {})

    def test_malformed_parts_are_refused_at_start(self):
        (self.tmp / "pat.md").write_text("x", encoding="utf-8")
        cases = {
            "is a list": {"text": "x"},
            "a part takes": [{"text": "x", "colour": "blue"}],
            "either `text` or `file`": [{"text": "x", "file": "pat.md"}],
            "either `text` or `file` ": [{"name": "empty"}],
            "`file` is a path": [{"file": ""}],
            "`text` is text": [{"text": 5}],
            "`order` is a whole number": [{"text": "x", "order": "first"}],
            "`order` is a whole number ": [{"text": "x", "order": True}],
            "`name` is text": [{"text": "x", "name": ""}],
            "already named": [{"name": "a", "text": "x"}, {"name": "a", "text": "y"}],
            "place is one of": [{"text": "x", "place": "middle"}],
        }
        for expected, parts in cases.items():
            with self.subTest(expected), self.assertRaises(ConfigError) as caught:
                self.config({"p": {"prompt": "You are Pat.", "parts": parts}})
            self.assertIn(expected.strip(), str(caught.exception))

    def test_an_entry_without_a_prompt_adds_parts_to_a_modules_persona(self):
        config = self.config({"luna": {"parts": [{"text": "Loves puns."}]}})
        self.assertNotIn("luna", config.personas)
        self.assertEqual(config.persona_parts, {"luna": (PromptPart("part 1", "Loves puns."),)})

    def test_an_entry_without_a_prompt_takes_only_parts(self):
        for value in ({"display_name": "L", "parts": []}, {"profile": "x"}, "a prompt"):
            with self.subTest(value), self.assertRaises(ConfigError):
                self.config({"luna": value})

    def test_the_built_in_assistant_keeps_its_prompt_when_core_yml_gives_it_parts(self):
        from core.defaults import DEFAULT_PROMPT
        path = self.tmp / "core.yml"
        # env_files: [] keeps the repository's own .env out of this process.
        path.write_text("env_files: []\npersonas:\n  assistant:\n    parts:\n"
                        "      - {name: personality, text: Cheerful.}\n", encoding="utf-8")
        persona = CoreConfig.load(path).personas["assistant"]
        self.assertEqual(persona.prompt, DEFAULT_PROMPT)
        self.assertEqual(persona.parts, (PromptPart("personality", "Cheerful."),))


class ContractTests(unittest.TestCase):
    def test_a_part_needs_a_name_a_place_text_and_a_whole_number_order(self):
        for arguments in (("", "x"), ("a", "x", "middle"), ("a", 5), ("a", "x", "stable", "10"),
                          ("a", "x", "stable", None), ("a", "x", "stable", True), ("a", "x", "stable", 1, [3])):
            with self.subTest(arguments), self.assertRaises(ValueError):
                PromptPart(*arguments)

    def test_a_parts_personas_are_names(self):
        self.assertEqual(PromptPart("a", "x", personas="luna").personas, ("luna",))
        self.assertEqual(PromptPart("a", "x", personas=["luna", "gemini"]).personas, ("luna", "gemini"))
        hash(PromptPart("a", "x", personas=["luna"]))  # frozen and hashable, whatever it was given

    def test_a_personas_parts_are_parts_with_distinct_names(self):
        part = PromptPart("a", "x")
        self.assertEqual(Persona("p", "Pat", "x", parts=[part]).parts, (part,))
        for parts in (part, ["text"], (part, PromptPart("a", "y"))):
            with self.subTest(parts), self.assertRaises(ValueError):
                Persona("p", "Pat", "x", parts=parts)


class RenderTests(Temporary):
    def render(self, parts, place="stable", owner=False):
        problems = []
        text = render(parts, context(owner=owner), place,
                      lambda part, kind, detail: problems.append((part.name, kind, detail)))
        return text, problems

    def test_parts_come_in_order_at_their_place_and_ties_keep_their_order(self):
        parts = [PromptPart("b", "B"), PromptPart("a", "A", order=10), PromptPart("c", "C"),
                 PromptPart("t", "T", place="turn"), PromptPart("blank", "  \n ")]
        self.assertEqual(self.render(parts), ("A\n\nB\n\nC", []))
        self.assertEqual(self.render(parts, "turn"), ("T", []))
        self.assertEqual(self.render([]), ("", []))
        with self.assertRaises(ValueError):
            self.render(parts, "middle")

    def test_a_computed_part_sees_the_run_and_a_failing_one_is_left_out(self):
        def boom(_):
            raise RuntimeError("no database")

        class Unprintable(Exception):
            def __str__(self):
                raise RuntimeError("cannot say")

        def unprintable(_):
            raise Unprintable()

        parts = [PromptPart("who", lambda c: f"{c.persona.display_name} talks with {c.trigger.sender.name}"
                                             + (" (the owner)" if c.owner else "")),
                 PromptPart("boom", boom), PromptPart("number", lambda c: 5), PromptPart("odd", unprintable)]
        text, problems = self.render(parts, owner=True)
        self.assertEqual(text, "Pat talks with Alice (the owner)")
        self.assertEqual(problems, [("boom", "failed", "left out: RuntimeError: no database"),
                                    ("number", "failed", "left out: TypeError: gave int, not text"),
                                    ("odd", "failed", "left out: Unprintable")])

    def test_an_async_part_is_left_out_without_a_warning(self):
        async def later(_):
            return "never"
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            text, problems = self.render([PromptPart("later", later)])
        self.assertEqual(text, "")
        self.assertIn("async", problems[0][2])

    def test_the_parts_at_one_place_keep_to_the_limit(self):
        parts = [PromptPart("big", "x" * (PLACE_LIMIT - 10)), PromptPart("next", "y" * 50), PromptPart("last", "z")]
        text, problems = self.render(parts)
        self.assertLessEqual(len(text), PLACE_LIMIT)
        self.assertTrue(text.endswith("y [...]"))
        self.assertNotIn("z", text)
        self.assertEqual([(name, kind) for name, kind, _ in problems], [("next", "cut"), ("last", "left_out")])

    def test_after_a_cut_nothing_more_is_added_even_if_the_cut_part_shrank(self):
        parts = [PromptPart("spaced", "a" * 100 + " " * PLACE_LIMIT + "b"), PromptPart("next", "y")]
        text, problems = self.render(parts)
        self.assertEqual(text, "a" * 100 + " [...]")
        self.assertEqual([(name, kind) for name, kind, _ in problems], [("spaced", "cut"), ("next", "left_out")])

    def test_a_file_part_is_read_each_time_without_its_comments(self):
        path = self.tmp / "voice.md"
        path.write_text("<!-- notes for the editor -->\nSpeak softly.<!--\nmore notes\n-->", encoding="utf-8")
        part = PromptPart("voice", FileText(path))
        self.assertEqual(self.render([part]), ("Speak softly.", []))
        path.write_text("Speak loudly.\n<!-- an unclosed note runs to the end", encoding="utf-8")
        self.assertEqual(self.render([part]), ("Speak loudly.", []))
        path.unlink()
        text, problems = self.render([part])
        self.assertEqual(text, "")
        self.assertIn("FileNotFoundError", problems[0][2])
        path.mkdir()  # not a regular file
        self.assertEqual(self.render([part])[1][0][1], "failed")

    def test_a_personas_own_part_shadows_a_modules_part_of_the_same_name(self):
        persona = Persona("p", "Pat", "You are Pat.", parts=(PromptPart("style", "Mine."),))
        shared = [PromptPart("style", "The module's."), PromptPart("rules", "For all."),
                  PromptPart("luna", "For Luna.", personas=("luna",)), PromptPart("pat", "For Pat.", personas=("p",)),
                  PromptPart("lun", "For Lun.", personas="lun")]
        self.assertEqual([part.text for part in parts_for(persona, shared)], ["Mine.", "For all.", "For Pat."])
        luna = Persona("luna", "Luna", "x")
        self.assertEqual([part.name for part in parts_for(luna, shared)], ["style", "rules", "luna"])

    def test_core_yml_parts_replace_a_modules_persona_parts_in_place(self):
        persona = Persona("luna", "Luna", "You are Luna.", "luna", (PromptPart("a", "1"), PromptPart("b", "2")))
        changed = with_parts(persona, [PromptPart("a", ""), PromptPart("c", "3")])
        self.assertEqual([(part.name, part.text) for part in changed.parts], [("a", ""), ("b", "2"), ("c", "3")])
        self.assertEqual((changed.prompt, changed.profile), ("You are Luna.", "luna"))


class PartsProbe(Engine):
    """Records what `Session.prompt` gives, and whether it refuses a persona that is not the run's."""

    spec = EngineSpec("parts_probe", "test engine", "one")
    seen: list = []

    async def run(self, session):
        persona = session.personas[0].name
        try:
            session.prompt("someone_else")
            refused = False
        except ValueError:
            refused = True
        self.seen.append((session.prompt(persona), session.prompt(persona, "turn"), refused))
        return RunYield.DONE


class AssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, parts, responses=None, overrides=None):
        adapter = FakeAdapter(responses or [reply()] * 6)
        persona = {"display_name": "Pat", "prompt": "You are Pat.", "parts": parts}
        core = await start_core(self.tmp, {**ASSISTANT, "personas": {"p": persona}, **(overrides or {})}, adapter)
        self.cores.append(core)
        return core, adapter

    async def say(self, core, *texts):
        for text in texts:
            await iface(core).say(text)
            await settle(core)

    async def test_stable_parts_go_in_the_system_prompt_and_turn_parts_in_the_turns_context(self):
        core, adapter = await self.core(
            [{"name": "personality", "text": "Cheerful, a little dry."},
             {"name": "today", "text": "It is Pat's birthday.", "place": "turn"}],
            overrides={"assignments": {"*": {"engine": "assistant", "personas": ["p"],
                                             "options": {"instructions": "Keep it short."}}}})
        await self.say(core, "hello")
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertTrue(system.startswith("You are Pat.\n\nYour name here is Pat.\n\nCheerful, a little dry.\n\n"
                                          "Instructions for this conversation:\nKeep it short."))
        self.assertNotIn("birthday", system)
        self.assertIn("It is Pat's birthday.", turn_context(adapter.calls[0]))

    async def test_without_parts_the_system_prompt_is_as_before(self):
        core, adapter = await self.core([])
        await self.say(core, "hello")
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertTrue(system.startswith("You are Pat.\n\nYour name here is Pat.\n\nRespond to every turn"),
                        system[:120])

    async def test_a_modules_parts_reach_every_persona_or_those_named_and_its_own_part_wins(self):
        core, adapter = await self.core([{"name": "style", "text": "Pat's own style."}])
        manager = core.manager
        manager.register_prompt_part(PromptPart("style", "The module's style."), "test")
        manager.register_prompt_part(PromptPart("house", lambda c: f"House rules for {c.trigger.sender.name}."), "test")
        manager.register_prompt_part(PromptPart("luna", "Only Luna.", personas=("luna",)), "test")
        with self.assertRaises(ValueError):
            manager.register_prompt_part(PromptPart("house", "Another module's house rules."), "other")
        await self.say(core, "hello")
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertIn("Pat's own style.\n\nHouse rules for Alice.", system)
        self.assertNotIn("The module's style.", system)
        self.assertNotIn("Only Luna.", system)

    async def test_a_modules_contributions_and_core_yml_parts_for_its_persona(self):
        module = self.tmp / "partsmod"
        module.mkdir()
        (module / "extension.py").write_text(textwrap.dedent(MODULE), encoding="utf-8")
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(module)]
        core, adapter = await self.core([], overrides={
            "extensions": extensions,
            "personas": {"p": {"display_name": "Pat", "prompt": "You are Pat."},
                         "ivy": {"parts": [{"name": "style", "text": "core.yml's style for Ivy."},
                                           {"name": "extra", "text": "Ivy likes tea."}]}},
            "assignments": {"*": {"engine": "assistant", "personas": ["p"]},
                            "chat:ivy": {"engine": "assistant", "personas": ["ivy"]}}})
        self.assertEqual([part.name for part in core.manager.personas["ivy"].parts], ["style", "habit", "extra"])
        await self.say(core, "hello")
        await iface(core).say("hi Ivy", conversation="ivy")
        await settle(core)
        pat, ivy = (call["messages"][0]["content"] for call in adapter.calls)
        self.assertIn("House rules for Pat.\n\nOnly for Pat.", pat)
        self.assertIn("Your name here is Ivy.\n\ncore.yml's style for Ivy.\n\nIvy hums.\n\nIvy likes tea.\n\n"
                      "House rules for Ivy.", ivy)
        self.assertNotIn("Ivy's own style.", ivy)
        self.assertNotIn("Only for Pat.", ivy)

    async def test_two_modules_may_not_add_parts_of_the_same_name(self):
        for name in ("one", "two"):
            module = self.tmp / name
            module.mkdir()
            (module / "extension.py").write_text(
                "from contracts import Contributions, Extension, PromptPart\n"
                f"EXTENSION = Extension({name!r}, lambda context: Contributions("
                "prompt_parts=[PromptPart('house', 'Rules.')]), contracts='1')\n", encoding="utf-8")
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(self.tmp / "one"), str(self.tmp / "two")]
        with self.assertRaises(ValueError) as caught:
            await self.core([], overrides={"extensions": extensions})
        self.assertIn("already registered by 'one'", str(caught.exception))

    async def test_an_edited_file_part_applies_from_the_next_turn(self):
        path = self.tmp / "pat.md"
        path.write_text("Speak softly.", encoding="utf-8")
        core, adapter = await self.core([{"file": str(path)}])
        await self.say(core, "hello")
        path.write_text("Speak loudly.", encoding="utf-8")
        await self.say(core, "again")
        self.assertIn("Speak softly.", adapter.calls[0]["messages"][0]["content"])
        self.assertIn("Speak loudly.", adapter.calls[1]["messages"][0]["content"])

    async def test_a_missing_file_is_a_warning_not_an_error_and_the_part_returns_with_it(self):
        path = self.tmp / "later.md"
        core, adapter = await self.core([{"file": "later.md"}])
        self.assertEqual(core.manager.prompt_part_warnings(),
                         [f"persona 'p', part 'later': no file {path} (left out until it exists)"])
        self.assertIn("no file", audited(core)[0]["problem"])
        await self.say(core, "hello")
        path.write_text("Here now.", encoding="utf-8")
        await self.say(core, "again")
        self.assertNotIn("Here now.", adapter.calls[0]["messages"][0]["content"])
        self.assertIn("Here now.", adapter.calls[1]["messages"][0]["content"])
        self.assertEqual(core.manager.prompt_part_warnings(), [])

    async def test_a_broken_part_is_audited_once_until_it_renders_again(self):
        core, adapter = await self.core([])

        def flaky(context):
            if context.trigger.text == "three":
                return "Fine this time."
            raise ValueError(f"on {context.trigger.text} at {time.time()}")  # a message that changes every time

        core.manager.register_prompt_part(PromptPart("flaky", flaky), "test")
        await self.say(core, "one", "two", "three", "four")
        self.assertEqual(len(adapter.calls), 4)  # every run answered
        self.assertIn("Fine this time.", adapter.calls[2]["messages"][0]["content"])
        events = audited(core)
        self.assertEqual([(event["part"], event["source"]) for event in events], [("flaky", "test")] * 2)
        self.assertIn("on one", events[0]["problem"])
        self.assertIn("on four", events[1]["problem"])

    async def test_secrets_are_scrubbed_from_parts(self):
        core, adapter = await self.core([{"text": "The key is sk-test-0123456789abcdef0123."}])
        core.manager.secrets = Secrets({"PAT_API_KEY": "sk-test-0123456789abcdef0123"})
        await self.say(core, "hello")
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertNotIn("0123456789abcdef", system)
        self.assertIn("[secret PAT_API_KEY]", system)

    async def test_a_file_part_on_a_secret_path_is_refused_at_start(self):
        (self.tmp / ".env").write_text("PAT_API_KEY=sk-test-0123456789abcdef0123\n", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            await self.core([{"file": str(self.tmp / ".env")}])
        self.assertIn("protected secret path", str(caught.exception))

    async def test_a_relative_secrets_entry_protects_the_file_beside_core_yml(self):
        (self.tmp / "personas").mkdir()
        (self.tmp / "personas" / "private.md").write_text("private", encoding="utf-8")
        with self.assertRaises(ConfigError) as caught:
            await self.core([{"file": "personas/private.md"}], overrides={"secrets": ["personas/private.md"]})
        self.assertIn("protected secret path", str(caught.exception))

    async def test_a_part_file_swapped_for_a_secret_after_start_is_left_out(self):
        secret = self.tmp / ".env"
        secret.write_text("PAT_TOKEN=abcdefghijklmnop0123456789\n", encoding="utf-8")
        path = self.tmp / "voice.md"
        path.write_text("Speak softly.", encoding="utf-8")
        core, adapter = await self.core([{"file": "voice.md"}])
        path.unlink()
        path.symlink_to(secret)
        await self.say(core, "hello")
        self.assertNotIn("abcdefghijklmnop", adapter.calls[0]["messages"][0]["content"])
        self.assertIn("protected secret path", audited(core)[-1]["problem"])

    async def test_parts_for_a_persona_no_module_has_are_refused_at_start(self):
        with self.assertRaises(ConfigError) as caught:
            await self.core([], overrides={"personas": {"p": {"prompt": "You are Pat."},
                                                        "nobody": {"parts": [{"text": "x"}]}}})
        self.assertIn("'nobody'", str(caught.exception))

    async def test_a_session_renders_only_its_own_personas(self):
        core, _ = await self.core([{"text": "Stable."}, {"text": "Turn.", "place": "turn"}])
        PartsProbe.seen = []
        core.manager.register_engine(PartsProbe(), "test")
        core.manager.set_assignment("chat:*", {"engine": "parts_probe", "personas": ["p"]})
        await self.say(core, "hello")
        self.assertEqual(PartsProbe.seen, [("Stable.", "Turn.", True)])


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, script, personas=None):
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "room")]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "personas": personas or {},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in (*NAMES, "grok")},
            "assignments": {"*": {"engine": "room", "personas": list(NAMES)}}}, FakeAdapter(handler=script))
        return iface(self.core)

    async def test_each_character_gets_the_parts_core_yml_adds_to_it(self):
        script = Script(Luna=[room_turn("Hi!")], Gemini=[room_turn("Hey.")])
        chat = await self.start(script, {"luna": {"parts": [{"name": "quirk", "text": "Luna loves puns."},
                                                             {"text": "Luna had tea.", "place": "turn"}]}})
        for text in ("luna, hi", "gemini, hi"):
            await chat.say(text)
            await settle(self.core)
        luna, gemini = script.prompts["Luna"][0], script.prompts["Gemini"][0]
        self.assertIn("Your name here is Luna.\n\nLuna loves puns.", luna[0]["content"])
        self.assertNotIn("Luna had tea.", luna[0]["content"])
        self.assertIn("Luna had tea.", luna[-1]["content"])
        self.assertNotIn("Luna loves puns", "\n".join(message["content"] for message in gemini))
        self.assertEqual(self.core.manager.personas["luna"].profile, "luna")  # the rest of her is the room's

    async def test_the_system_prompt_holds_no_time_so_it_stays_the_same_turn_to_turn(self):
        script = Script(Luna=[room_turn("Hi!"), room_turn("Again!")])
        chat = await self.start(script)
        for text in ("luna, hi", "luna, again"):
            await chat.say(text)
            await settle(self.core)
        first, second = script.prompts["Luna"][0], script.prompts["Luna"][1]
        self.assertNotIn("The current time is", first[0]["content"])
        self.assertIn("[This turn's context", first[-1]["content"])
        self.assertIn("The current time is", first[-1]["content"])
        self.assertEqual(first[0], second[0])


class WhaleChanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import os
        from unittest.mock import patch
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None
        environment = patch.dict(os.environ, {"DISCORD_BOT_TOKEN_WHALE_CHAN": "", "OPENAI_API_KEY": ""})
        environment.start()
        self.addCleanup(environment.stop)

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def test_her_parts_go_beside_her_prompt_and_in_the_turns_context(self):
        from tests.test_plugin_whale_chan import PLUGIN, QUIET, step
        adapter = FakeAdapter([step("嗨～")])
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "profiles": {"whale_chan": [{"provider": "fake", "model": "pro"}]},
            "extension_config": {"whale_chan": QUIET},
            "personas": {"whale_chan": {"parts": [{"name": "personality", "text": "Teases gently."},
                                                  {"name": "season", "text": "It is autumn.", "place": "turn"}]}},
            "assignments": {"*": {"engine": "chat", "personas": ["whale_chan"]}}}, adapter)
        await iface(self.core).say("你好")
        await settle(self.core)
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertIn("\n\nYour name here is", system)
        self.assertIn("Teases gently.", system.split("Your name here is", 1)[1])
        self.assertIn("本鲸", system)  # her own prompt is kept
        self.assertNotIn("It is autumn.", system)
        self.assertIn("It is autumn.", turn_context(adapter.calls[0]))


if __name__ == "__main__":
    unittest.main()

"""Contracts 1.11: engines and modules keep what was theirs under a name they had before (EngineSpec.formerly,
Extension.formerly), and agents are described for other engines (Persona.description, Persona.profiles,
ExtensionContext.personas)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from contracts import Contributions, EngineSpec, Extension, Persona
from core.app import Core
from core.manager.policy import Assignment
from core.storage.tank import RunRecord
from tests.core_support import FIXTURES, FakeAdapter, iface, make_config, settle

# A module of the tests' own: an engine that echoes (named `talk`, formerly `chatter`), a persona, and what it saw of
# the agents and its settings at setup.
MODULE = """
from contracts import Contributions, Engine, EngineSpec, Extension, Persona, RunYield
SEEN = {{}}


class Talk(Engine):
    spec = EngineSpec("talk", "echoes", "one", formerly=("chatter",))

    async def run(self, session):
        await session.reply("talk: " + session.trigger.text + (" (resumed)" if session.state.get("old") else ""))
        return RunYield.DONE


def setup(context):
    SEEN["agents"] = {{view["name"]: view for view in context.personas()}}
    SEEN["config"] = dict(context.config)
    SEEN["present"] = sorted(context.present)
    SEEN["context"] = context
    return Contributions(engines=[Talk()] if context.name == "talker" else []{contributions})


EXTENSION = Extension({name!r}, setup, contracts="1"{declared})
"""


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            if core.started:
                await core.stop()
            else:
                core.tank.close()
                core.bottle.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def module(self, name, contributions="", declared="", config=None):
        folder = self.tmp / "modules" / name
        folder.mkdir(parents=True)
        (folder / "extension.py").write_text(MODULE.format(name=name, contributions=contributions, declared=declared),
                                             encoding="utf-8")
        if config:
            (folder / "config.yml").write_text(json.dumps(config), encoding="utf-8")
        return str(folder)

    async def build(self, *modules, start=False, **overrides):
        config = make_config(self.tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"), *modules],
                                        **overrides})
        core = Core(config, provider_adapters={"fake": FakeAdapter()}, tick_seconds=3600)
        self.cores.append(core)
        await (core.start() if start else core.build())
        return core

    @staticmethod
    def seen(core, name):
        return next(item.module for item in core.extensions if item.extension.name == name).SEEN


class DeclarationTests(unittest.TestCase):
    def test_former_names_and_descriptions_are_checked(self):
        self.assertEqual(EngineSpec("momochat", formerly=["room", "chat"]).formerly, ("room", "chat"))
        with self.assertRaisesRegex(ValueError, "its own name"):
            EngineSpec("room", formerly=("room",))
        setup = lambda context: Contributions()
        self.assertEqual(Extension("students", setup, formerly=["companion"]).formerly, ("companion",))
        with self.assertRaisesRegex(ValueError, "other module names"):
            Extension("students", setup, formerly=("students",))
        gpt = Persona("gpt", "GPT", "…", "gpt", description=" GPT, the\ncalm president ",
                      profiles={"own": "gpt_own", "strong": "gpt_strong"})
        self.assertEqual(gpt.description, "GPT, the calm president")
        self.assertEqual((gpt.tier("strong"), gpt.tier("own")), ("gpt_strong", "gpt_own"))
        self.assertEqual(Persona("x", "X", "…", "x", profiles={"own": "x_own"}).tier("strong"), "x_own")
        self.assertEqual(Persona("x", "X", "…", "x").tier("strong"), "x")
        with self.assertRaisesRegex(ValueError, "tier"):
            Persona("x", "X", "…", profiles={"fast": "x_fast"})


class EngineNameTests(Base):
    async def test_an_assignment_naming_an_engine_by_its_old_name_reaches_it(self):
        core = await self.build(self.module("talker"), start=True,
                                assignments={"chat:room": {"engine": "chatter", "personas": ["p"]}})
        self.assertEqual(core.manager.assignments["chat:room"].engine, "talk")
        self.assertTrue(any(event["event"] == "engine_renamed_in_config" and event["old"] == "chatter"
                            for event in core.audit.events))
        await iface(core).say("hello", conversation="room", sender="boss")
        await settle(core)
        self.assertIn("talk: hello", iface(core).texts("room"))

    async def test_open_runs_of_the_old_engine_carry_on_in_the_new_one(self):
        core = await self.build(self.module("talker"))
        record = RunRecord(id="old1", address="chat:room", lane="chat:room", engine="chatter", personas=["p"],
                           assignment=Assignment.from_config("chat:room", {"engine": "chatter", "personas": ["p"]}
                                                             ).to_dict(), status="running", origin="person",
                           trigger={"id": "m1", "address": "chat:room",
                                    "sender": {"id": "boss", "name": "boss"}, "text": "again", "received_at": 1.0},
                           state={"old": True})
        core.tank.create_run(record)
        core.tank.close()
        core.bottle.close()
        self.cores.remove(core)
        core = await self.build(self.module_again("talker"), start=True)
        await settle(core)
        self.assertEqual(core.tank.run("old1").engine, "talk")
        self.assertIn("talk: again (resumed)", iface(core).texts("room"))

    def module_again(self, name):
        return str(self.tmp / "modules" / name)


class ModuleNameTests(Base):
    async def test_a_module_takes_over_the_data_settings_stores_and_facts_of_its_former_name(self):
        data = self.tmp / "data" / "extensions" / "companion"
        data.mkdir(parents=True)
        (data / "gpt.db").write_text("memory", encoding="utf-8")
        old = self.tmp / "modules" / "companion"
        old.mkdir(parents=True)
        (old / "config.yml").write_text(json.dumps({"learn": False, "mood": False}), encoding="utf-8")
        core = await self.build(self.module("students", declared=', formerly=("companion",)', config={"mood": True}),
                                extension_config={"companion": {"notes": False}})
        self.assertEqual((self.tmp / "data" / "extensions" / "students" / "gpt.db").read_text(), "memory")
        self.assertFalse(data.exists())
        config = self.seen(core, "students")["config"]
        self.assertEqual((config["learn"], config["mood"], config["notes"]), (False, True, False))
        self.assertTrue(any(event["event"] == "module_settings_moved" for event in core.audit.events))

    async def test_stores_and_facts_move_on_the_next_start(self):
        core = await self.build(self.module("companion"))
        core.tank.store("extension:companion").set("seen", 1)
        core.tank.store('extension:companion {"persona": "p"}').set("mood", 3)
        core.manager.facts.handle("companion").set("p", "bond", {"role": "friend"})
        core.tank.close()
        core.bottle.close()
        self.cores.remove(core)
        shutil.rmtree(self.tmp / "modules" / "companion")
        core = await self.build(self.module("students", declared=', formerly=("companion",)'))
        self.assertEqual(core.tank.store("extension:students").get("seen"), 1)
        self.assertEqual(core.tank.store('extension:students {"persona": "p"}').get("mood"), 3)
        self.assertEqual(core.manager.facts.read("p"), {"students.bond": {"role": "friend"}})
        self.assertTrue(any(event["event"] == "module_renamed" for event in core.audit.events))

    async def test_a_declaration_naming_the_old_name_is_given_the_module(self):
        core = await self.build(self.module("page", declared=', requires=("room",), uses=("companion",)'),
                                self.module("momochat", declared=', formerly=("room", "assistant")'),
                                self.module("students", declared=', formerly=("companion",)'))
        names = [item.extension.name for item in core.extensions]
        self.assertLess(names.index("momochat"), names.index("page"))
        self.assertLess(names.index("students"), names.index("page"))
        self.assertEqual(self.seen(core, "page")["present"], ["companion"])
        self.assertEqual(core.skipped, {})


class AgentTests(Base):
    async def test_a_module_sees_the_agents_loaded_before_it_with_their_models(self):
        core = await self.build(
            self.module("students", ', personas=[Persona("gpt", "GPT", "…", "gpt", description="GPT, the president", '
                                    'profiles={"own": "gpt"})], profiles={"gpt": [{"provider": "fake", "model": "g"}]}'),
            self.module("chimera", declared=', uses=("students",)'),
            profiles={"default": [{"provider": "fake", "model": "fake-model"}],
                      "gpt": [{"provider": "fake", "model": "sol"}]})  # core.yml's wins
        agents = self.seen(core, "chimera")["agents"]
        self.assertEqual(agents["gpt"]["module"], "students")
        self.assertEqual(agents["gpt"]["description"], "GPT, the president")
        self.assertEqual(agents["gpt"]["models"]["strong"], [{"provider": "fake", "model": "sol"}])
        self.assertEqual(agents["p"]["module"], "core")  # core.yml's agent
        self.assertNotIn("chimera", self.seen(core, "students")["agents"])
        later = {view["name"] for view in self.seen(core, "students")["context"].personas()}
        self.assertEqual(later, {"p", "gpt"})  # after setup: every agent

    async def test_core_yml_defines_an_agent_with_her_models_in_one_entry(self):
        core = await self.build(start=True, agents={
            "helper": {"display_name": "Helper", "prompt": "You help.", "models": ["fake/small", "fake/large"],
                       "description": "A helpful agent", "aliases": ["Help"]},
            "p": {"parts": [{"name": "extra", "text": "Be brief."}]}},  # parts for core.yml's other agent, as before
            assignments={"chat:help": {"engine": "probe", "personas": ["helper"]}})
        helper = core.manager.personas["helper"]
        self.assertEqual((helper.profile, helper.aliases, helper.description), ("helper", ("Help",), "A helpful agent"))
        self.assertEqual(core.providers.candidates("helper"), [{"provider": "fake", "model": "small"},
                                                               {"provider": "fake", "model": "large"}])
        self.assertEqual([part.name for part in core.manager.personas["p"].parts], ["extra"])
        from core.config import ConfigError
        with self.assertRaisesRegex(ConfigError, "`models` or a `profile`"):
            await self.build(agents={"x": {"prompt": "…", "models": ["fake/a"], "profile": "default"}})


if __name__ == "__main__":
    unittest.main()

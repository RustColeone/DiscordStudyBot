"""Contracts 1.9: watchers, facts, InterfacePort.personas, and a persona's former names (Persona.formerly)."""

import asyncio
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from contracts import Address, Exchange, Persona, Request, Sender, Watcher
from core.config import ConfigError
from tests.core_support import FakeAdapter, iface, make_config, settle, start_core

# A module with a watcher that keeps what it saw, calls a model on "think", and publishes facts.
MODULE = '''
import asyncio

from contracts import Contributions, Extension, ModelRequest, Persona, Watcher

SEEN = []


async def watch(exchange, context):
    SEEN.append(exchange)
    if exchange.trigger.text == "echo boom":
        raise RuntimeError("boom")
    if exchange.trigger.text == "echo slow":
        await asyncio.sleep(5)
    if exchange.trigger.text == "echo think":
        result = await context.models.generate(ModelRequest([{"role": "user", "content": "note it"}]))
        context.facts.set(exchange.persona.name, "noted", result.text, person=exchange.person)
    context.facts.set(exchange.persona.name, "seen", len(SEEN))
    count = context.store(persona=True, person=True)
    count.set("times", (count.get("times") or 0) + 1)
    context.log("watched", persona=exchange.persona.name)


def setup(context):
    context.facts.set("ivy", "mood", "sunny")
    return Contributions(
        personas=[Persona("ivy", "Ivy", "You are Ivy.", aliases=("Ives",), pictures={"avatar": "ivy.png"})],
        watchers=[Watcher("keeper", watch, timeout_s=0.5), Watcher("ivy_only", watch, personas=("ivy",))])


EXTENSION = Extension("watchmod", setup, contracts="1")
'''


def module_folder(tmp: Path, source: str = MODULE, name: str = "watchmod") -> Path:
    folder = tmp / name
    folder.mkdir(exist_ok=True)
    (folder / "extension.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return folder


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, overrides=None, adapter=None, module=MODULE, folder="watchmod"):
        # (a module is imported once per folder: another version of it needs a folder of its own)
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(module_folder(self.tmp, module, folder))]
        core = await start_core(self.tmp, {"extensions": extensions, **(overrides or {})}, adapter)
        self.cores.append(core)
        self.seen = next(item.module for item in core.extensions if item.extension.name == "watchmod").SEEN
        self.seen.clear()
        return core

    async def say(self, core, *texts, conversation="room"):
        for text in texts:
            await iface(core).say(text, conversation=conversation)
            await settle(core)
        await core.manager.idle()


class WatcherTests(Base):
    async def test_a_watcher_sees_each_delivered_message_with_what_it_answered(self):
        core = await self.core()
        await self.say(core, "echo hello")
        exchange = self.seen[0]
        self.assertIsInstance(exchange, Exchange)
        self.assertEqual((exchange.persona.name, exchange.text, exchange.trigger.text, exchange.engine),
                         ("p", "hello", "echo hello", "probe"))
        self.assertEqual(exchange.person, ("chat", "alice"))
        self.assertEqual(exchange.personas, ("p",))  # who takes part in the conversation: the run's personas
        self.assertEqual(len(self.seen), 1)  # `ivy_only` names only Ivy
        run = core.tank.runs(("done",))[0]
        times = core.tank.store('extension:watchmod {"person": "[\\"chat\\", \\"alice\\"]", "persona": "p"}')
        self.assertEqual(times.get("times"), 1)
        self.assertIn("watched", [event.get("detail") for event in core.audit.events if event["event"] == "watcher"])
        self.assertEqual(run.usage.get("watcher_calls", 0), 0)

    async def test_a_question_waiting_for_input_counts_as_said_and_command_output_does_not(self):
        core = await self.core()
        await self.say(core, "$help")
        self.assertEqual(self.seen, [])
        await self.say(core, "ask")
        self.assertEqual([exchange.text for exchange in self.seen], ["which one?"])

    async def test_a_failing_or_slow_watcher_is_only_audited(self):
        core = await self.core()
        await self.say(core, "echo boom", "echo slow")
        failures = [event for event in core.audit.events if event["event"] == "watcher_failed"]
        self.assertEqual([(event["watcher"], event["error"]) for event in failures],
                         [("keeper", "RuntimeError: boom"), ("keeper", "timeout")])
        self.assertEqual(iface(core).texts(), ["boom", "slow"])  # the conversation went on

    async def test_a_watchers_model_calls_count_in_usage_but_not_against_the_runs_budget(self):
        adapter = FakeAdapter([{"message": "she likes tea"}])
        core = await self.core({"assignments": {"*": {"engine": "probe", "personas": ["p"],
                                                      "budget": {"model_calls": 0}}}}, adapter)
        await self.say(core, "echo think")
        run = core.tank.runs(("done",))[0]
        self.assertEqual(run.usage.get("watcher_calls"), 1)
        self.assertEqual(run.usage.get("model_calls", 0), 0)
        port = iface(core).port
        self.assertEqual(port.facts("p", "alice")["watchmod.noted"], '{"message": "she likes tea"}')

    async def test_two_modules_cannot_register_one_watcher_name(self):
        core = await self.core()
        with self.assertRaises(ValueError):
            core.manager.register_watcher(Watcher("keeper", lambda exchange, context: None), "other")

    async def test_a_contribution_that_is_not_a_watcher_is_refused(self):
        source = MODULE.replace('watchers=[Watcher("keeper", watch, timeout_s=0.5), '
                                'Watcher("ivy_only", watch, personas=("ivy",))]', 'watchers=["keeper"]')
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(module_folder(self.tmp, source, "watchmod"))]
        core = await start_core(self.tmp, {"extensions": extensions})
        self.cores.append(core)
        self.assertIn("not a contracts.Watcher", core.skipped["watchmod"])

    def test_a_watcher_checks_its_fields(self):
        async def run(exchange, context):
            return None
        for bad in ({"name": "Bad Name"}, {"personas": [""]}, {"timeout_s": 0}):
            with self.assertRaises(ValueError):
                Watcher(**{"name": "ok", "run": run, **bad})
        self.assertEqual(Watcher("ok", run, personas="ivy").personas, ("ivy",))


class FactsAndPersonasTests(Base):
    async def test_facts_are_the_modules_own_and_an_interface_reads_only_its_own_people(self):
        core = await self.core()
        await self.say(core, "echo hi")
        port = iface(core).port
        self.assertEqual(port.facts("ivy"), {"watchmod.mood": "sunny"})
        self.assertEqual(port.facts("p"), {"watchmod.seen": 1})
        handle = core.manager.facts.handle("watchmod")
        handle.set("p", "bond", {"closeness": 40}, person=("chat", "alice"))
        handle.set("p", "bond", {"closeness": 90}, person=("owner", "alice"))
        self.assertEqual(port.facts("p", "alice"), {"watchmod.seen": 1, "watchmod.bond": {"closeness": 40}})
        self.assertEqual(iface(core, "owner").port.facts("p", "alice")["watchmod.bond"], {"closeness": 90})
        self.assertEqual(handle.get("p", "bond", person=("chat", "alice")), {"closeness": 40})
        self.assertIsNone(core.manager.facts.handle("other").get("p", "bond", person=("chat", "alice")))
        handle.set("p", "bond", None, person=("chat", "alice"))
        self.assertNotIn("watchmod.bond", port.facts("p", "alice"))
        for bad in (("p", "Bad-Key", 1, None), ("p", "big", "x" * 5000, None), ("", "k", 1, None),
                    ("p", "k", object(), None), ("p", "k", 1, ("chat",))):
            with self.assertRaises(ValueError):
                handle.set(bad[0], bad[1], bad[2], person=bad[3])

    async def test_an_interface_sees_every_persona_with_her_names_and_pictures(self):
        core = await self.core()
        views = {view["name"]: view for view in iface(core).port.personas()}
        self.assertEqual([name for name in views if name in ("p", "ivy")], ["p", "ivy"])
        ivy = views["ivy"]
        self.assertEqual((ivy["display_name"], ivy["aliases"], ivy["formerly"]), ("Ivy", ["Ives"], []))
        self.assertTrue(Path(ivy["pictures"]["avatar"]).is_absolute())
        self.assertTrue(views["p"]["ready"])

    def test_a_persona_checks_her_names_and_pictures(self):
        self.assertEqual(Persona("a", "A", "", aliases="Ay").aliases, ("Ay",))
        for bad in ({"aliases": [""]}, {"formerly": ["a"]}, {"formerly": [3]}, {"pictures": {"avatar": ""}},
                    {"pictures": ["a.png"]}):
            with self.assertRaises(ValueError):
                Persona("a", "A", "", **bad)


class FormerNameTests(Base):
    """A persona renamed (luna → gpt) keeps everything the core filed under her old name."""

    MODULE = '''
from contracts import Contributions, Extension, Persona

NAME = {name!r}
FORMERLY = {formerly!r}


def setup(context):
    return Contributions(personas=[Persona(NAME, NAME.upper(), "You are " + NAME + ".", formerly=FORMERLY)])


EXTENSION = Extension("watchmod", setup, contracts="1")
SEEN = []
'''

    OVERRIDES = {"assignments": {"*": {"engine": "probe", "personas": ["luna"]}},
                 "personas": {"luna": {"parts": [{"name": "habit", "text": "She hums."}]}}}

    async def test_her_data_moves_to_her_new_name_once(self):
        core = await self.core({"assignments": {"*": {"engine": "probe", "personas": ["luna"]}}},
                               module=self.MODULE.format(name="luna", formerly=()), folder="luna")
        await self.say(core, "echo before")
        manager, tank = core.manager, core.tank
        tank.record_message("chat:room", "out", "I hum.", sender_name="Luna")
        tank.store("persona:luna").set("rotation", 3)
        tank.store('extension:room {"persona": "luna"}').set("seat", "front")
        tank.store('extension:room {"persona": "gpt"}').set("seat", "back")  # hers already: it wins
        tank.save_note("luna", "chat:room", "alice", "tea", "fact", "Alice likes tea.", 4, keep=20)
        manager.model_choices.set("chat:room", "luna", "fake/strong")
        manager._overrides.set("chat:side", {"pattern": "chat:side", "engine": "probe", "personas": ["luna"]})
        manager.facts.handle("watchmod").set("luna", "mood", "calm", person=("chat", "alice"))
        await core.stop()
        self.cores.remove(core)

        core = await self.core(self.OVERRIDES, module=self.MODULE.format(name="gpt", formerly=("luna",)),
                               folder="gpt")
        manager, tank = core.manager, core.tank
        self.assertEqual(manager.assignments["*"].personas, ("gpt",))
        self.assertEqual(manager.assignments["chat:side"].personas, ("gpt",))
        self.assertEqual([part.name for part in manager.personas["gpt"].parts], ["habit"])
        self.assertEqual(tank.store("persona:gpt").get("rotation"), 3)
        self.assertEqual(tank.store('extension:room {"persona": "gpt"}').get("seat"), "back")
        self.assertEqual(tank.store("persona:luna").keys(), [])
        self.assertEqual([note.note for note in tank.notes("gpt", "chat:room", "alice")], ["Alice likes tea."])
        self.assertEqual(manager.model_choices.get("chat:room", "gpt"), "fake/strong")
        self.assertEqual(iface(core).port.facts("gpt", "alice"), {"watchmod.mood": "calm"})
        self.assertEqual([run.personas for run in tank.runs(("done",))], [["gpt"]])
        said = [(item.sender_name, item.text) for item in tank.recent_messages("chat:room") if item.direction == "out"]
        self.assertEqual(said, [("", "before"), ("GPT", "I hum.")])
        moved = [event for event in core.audit.events if event["event"] == "persona_data_moved"]
        self.assertEqual((moved[0]["stores"], moved[0]["notes"], moved[0]["runs"], moved[0]["messages"],
                          moved[0]["facts"]), (2, 1, 1, 1, 1))
        self.assertTrue(any(event["event"] == "persona_renamed_in_config" for event in core.audit.events))
        await self.say(core, "echo after")
        self.assertEqual(iface(core).texts()[-1], "after")
        await core.stop()
        self.cores.remove(core)

        core = await self.core(self.OVERRIDES, module=self.MODULE.format(name="gpt", formerly=("luna",)),
                               folder="gpt")
        self.assertFalse(any(event["event"] == "persona_data_moved" for event in core.audit.events))

    async def test_a_former_name_another_persona_has_now_is_left_alone(self):
        source = self.MODULE.format(name="gpt", formerly=("p",))
        core = await self.core(module=source)
        self.assertEqual(core.manager.assignments["*"].personas, ("p",))

    async def test_an_old_name_nobody_had_is_still_an_error(self):
        with self.assertRaises(ConfigError):
            await self.core({"assignments": {"*": {"engine": "probe", "personas": ["sol"]}}},
                            module=self.MODULE.format(name="gpt", formerly=("luna",)), folder="gpt")


if __name__ == "__main__":
    unittest.main()

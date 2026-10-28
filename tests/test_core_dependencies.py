"""Contracts 1.10: modules declare what they build on (`requires`, `uses`); the core loads them in that order, leaves
out a module that cannot load (with the reason) and keeps running, and starts with no module at all."""

import shutil
import tempfile
import unittest
from pathlib import Path

from contracts import Contributions, Extension
from core.app import Core
from core.config import ConfigError, CoreConfig
from core.defaults import default_config, merge
from core.dependencies import load_order, missing_package
from core.manager.manager import NOTHING_ANSWERS
from tests.core_support import FIXTURES, FakeAdapter, iface, make_config, settle

# A module that records which of the modules it uses were there; `Page` is an interface of its own.
MODULE = """
from contracts import Contributions, Delivery, Display, Extension, Interface
SEEN = {{}}


class Page(Interface):
    display = Display()

    def __init__(self, name):
        self.name = name

    async def start(self, port):
        self.port = port

    async def stop(self):
        pass

    async def deliver(self, reply):
        return Delivery(True)


def setup(context):
    SEEN["present"] = sorted(context.present)
    return Contributions({contributions})


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

    def module(self, name, contributions="", declared="", body=None):
        folder = self.tmp / "modules" / name
        folder.mkdir(parents=True)
        source = body or MODULE.format(name=name, contributions=contributions, declared=declared)
        (folder / "extension.py").write_text(source, encoding="utf-8")
        return str(folder)

    async def build(self, *modules, **overrides):
        config = make_config(self.tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"), *modules],
                                        **overrides})
        core = Core(config, provider_adapters={"fake": FakeAdapter()}, tick_seconds=3600)
        self.cores.append(core)
        await core.build()
        return core

    @staticmethod
    def seen(core, name):
        return next(item.module for item in core.extensions if item.extension.name == name).SEEN


class DeclarationTests(unittest.TestCase):
    def test_the_declarations_are_checked(self):
        setup = lambda context: Contributions()
        self.assertEqual(Extension("a", setup, requires=["b", "package:mneme-llm>=0.2"]).requires,
                         ("b", "package:mneme-llm>=0.2"))
        for kwargs, message in (({"requires": "b"}, "not one string"), ({"uses": ["a"]}, "itself"),
                                ({"uses": ["package:x"]}, "module names"), ({"requires": ["B!"]}, "module names"),
                                ({"requires": ["b"], "uses": ["b"]}, "both required and used")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                Extension("a", setup, **kwargs)

    def test_modules_load_after_what_they_build_on_and_cycles_are_found(self):
        setup = lambda context: Contributions()
        extensions = {"web": Extension("web", setup, uses=("room", "missing")),
                      "room": Extension("room", setup, requires=("students",)),
                      "students": Extension("students", setup),
                      "x": Extension("x", setup, requires=("y",)), "y": Extension("y", setup, uses=("x",))}
        order, cyclic = load_order(extensions)
        self.assertEqual(order, ["students", "room", "web"])
        self.assertEqual(set(cyclic), {"x", "y"})
        self.assertIn("dependency cycle", cyclic["x"])

    def test_a_required_package_and_its_version(self):
        self.assertIsNone(missing_package("package:PyYAML"))
        self.assertIn("not installed", missing_package("package:no-such-package-here"))
        self.assertIn("999 or later", missing_package("package:PyYAML>=999"))


class LoadingTests(Base):
    async def test_a_module_is_told_which_of_the_modules_it_uses_are_there(self):
        core = await self.build(self.module("web", declared=', uses=("room", "talk")'), self.module("talk"))
        self.assertEqual(self.seen(core, "web")["present"], ["talk"])
        names = [item.extension.name for item in core.extensions]
        self.assertLess(names.index("talk"), names.index("web"))  # though "talk" comes later in the list
        self.assertEqual(core.skipped, {})

    async def test_without_what_it_requires_a_module_is_left_out_and_so_is_what_requires_it(self):
        core = await self.build(self.module("room", declared=', requires=("students",)'),
                                self.module("web", declared=', requires=("room",)'),
                                self.module("box", declared=', requires=("package:no-such-package-here",)'))
        self.assertIn("requires the students module", core.skipped["room"])
        self.assertIn("requires the room module, which was not loaded", core.skipped["web"])
        self.assertIn("no-such-package-here", core.skipped["box"])
        self.assertNotIn("room", [item.extension.name for item in core.extensions])
        self.assertEqual([event["extension"] for event in core.audit.events if event["event"] == "module_skipped"],
                         ["room", "web", "box"])

    async def test_a_module_whose_setup_fails_is_left_out_and_the_core_runs(self):
        broken = self.module("broken", body="from contracts import Extension\n"
                                            "def setup(context):\n    raise RuntimeError('no disk')\n"
                                            "EXTENSION = Extension('broken', setup, contracts='1')\n")
        core = await self.build(broken)
        self.assertIn("setup failed: RuntimeError: no disk", core.skipped["broken"])
        await core.start()
        await iface(core).say("hello", sender="boss")
        await settle(core)
        self.assertTrue(core.tank.runs(("done",)))  # the probe engine still answers

    async def test_a_default_naming_an_undeclared_module_refuses_the_module(self):
        core = await self.build(self.module(
            "page", 'interfaces=[Page("page")], assignments={"page:*": {"engine": "probe", "personas": ["p"]}}'))
        self.assertIn("names the engine 'probe' of the probe module, which it does not declare", core.skipped["page"])
        self.assertNotIn("page", core.gateway.interfaces)

    async def test_a_default_naming_a_missing_module_it_uses_is_set_aside(self):
        core = await self.build(self.module(
            "page", 'interfaces=[Page("page")], assignments={"page:*": {"engine": "room", "personas": ["p"]}}',
            declared=', uses=("room",)'))
        self.assertIn("page", core.gateway.interfaces)
        self.assertNotIn("page:*", core.manager.assignments)
        self.assertEqual(core.set_aside, [("page", "page:*", "the engine 'room' is not loaded (without room)")])
        self.assertTrue(any(event["event"] == "default_unusable" for event in core.audit.events))

    async def test_an_owner_assignment_naming_something_missing_still_stops_the_core(self):
        with self.assertRaisesRegex(ConfigError, "engine 'room' is not registered"):
            await self.build(assignments={"chat:room": {"engine": "room", "personas": ["p"]}})


class MinimumCoreTests(Base):
    def config(self, modules):
        raw = merge(default_config(), {
            "data_dir": str(self.tmp / "data"), "env_files": [], "owners": {"chat": ["boss"]},
            "extension_config": {"fake": {"interfaces": {"chat": {}}}},
            "modules": {"dirs": [], "paths": modules}})
        config = CoreConfig(raw, self.tmp)
        config.file, config.env_file = self.tmp / "core.yml", self.tmp / ".env"
        return config

    async def test_the_core_starts_with_no_module(self):
        core = Core(self.config([]), tick_seconds=3600)
        self.cores.append(core)
        await core.start()
        self.assertEqual(core.manager.engines, {})
        self.assertEqual({pattern for _, pattern, _ in core.set_aside}, {"*", "routine:*"})
        self.assertTrue(any(name.startswith("core.") for name in core.manager.capabilities))

    async def test_commands_work_without_an_engine_and_the_owner_is_told_once_why_nothing_answers(self):
        core = Core(self.config([str(FIXTURES / "fake")]), tick_seconds=3600)
        self.cores.append(core)
        await core.start()
        chat = iface(core)
        await chat.say("$help", conversation="dm-boss", sender="boss")
        await settle(core)
        self.assertTrue(any("$approve" in text for text in chat.texts("dm-boss")))
        for text in ("hello?", "anyone?"):
            await chat.say(text, conversation="dm-boss", sender="boss")
            await settle(core)
        await chat.say("hi", conversation="dm-alice", sender="alice")
        await settle(core)
        self.assertEqual(chat.texts("dm-boss").count(NOTHING_ANSWERS), 1)
        self.assertEqual(chat.texts("dm-alice"), [])


if __name__ == "__main__":
    unittest.main()

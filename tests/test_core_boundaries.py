import ast
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from contracts import Address, Capability, FileRef, Reply, Risk, ToolResult
from core.app import Core
from core.config import ConfigError
from core.loader import ExtensionError, import_extension
from core.storage.bottle import Bottle
from core.storage.tank import RunRecord, Tank
from tests.core_support import FakeAdapter, ROOT, make_config

THIRD_PARTY_ALLOWED = {"yaml", "jsonschema", "openai", "google", "anthropic"}


def imports(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(((node.module or "").split(".")[0], node.level))
    return found


class BoundaryTests(unittest.TestCase):
    def test_contracts_depend_only_on_the_standard_library(self):
        for path in (ROOT / "contracts").glob("*.py"):
            for module, level in imports(path):
                self.assertTrue(level or module == "contracts" or module in sys.stdlib_module_names,
                                f"{path.name} imports {module}")

    def test_core_never_imports_extensions(self):
        for path in (ROOT / "core").rglob("*.py"):
            for module, level in imports(path):
                if level:
                    continue
                self.assertNotIn(module, {"extensions", "plugins", "tests"}, f"{path} imports {module}")
                self.assertTrue(module in {"core", "contracts"} | THIRD_PARTY_ALLOWED
                                or module in sys.stdlib_module_names, f"{path} imports {module}")

    def test_extensions_see_the_core_only_through_contracts(self):
        roots = [ROOT / "extensions", ROOT / "plugins", ROOT / "tests" / "core_fixtures"]
        for root in roots:
            for path in root.rglob("*.py"):
                for module, level in imports(path):
                    if level:
                        continue
                    self.assertNotIn(module, {"core", "extensions", "plugins", "tests"}, f"{path} imports {module}")


def new_core(config):
    return Core(config, provider_adapters={"fake": FakeAdapter()})


def close(core):
    for name in ("tank", "bottle", "audit"):
        if hasattr(core, name):
            getattr(core, name).close()


class LoaderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def extension(self, name, body, helper=None):
        directory = self.tmp / "repo" / name
        directory.mkdir(parents=True)
        (directory / "extension.py").write_text(textwrap.dedent(body))
        if helper:
            (directory / "helper.py").write_text(textwrap.dedent(helper))
        return directory

    async def test_external_extension_with_relative_imports_registers_capabilities(self):
        path = self.extension("music_ext", """
            from contracts import Capability, Contributions, Extension, ToolResult
            from .helper import greeting

            async def hello(arguments, context):
                return ToolResult.ok(greeting())

            EXTENSION = Extension("music", lambda ctx: Contributions(capabilities=[
                Capability("music.hello", "Say hello", hello)]))
            """, "def greeting():\n    return 'hi from helper'\n")
        core = new_core(make_config(self.tmp, {"extensions": [*make_config(self.tmp).raw["modules"]["paths"], str(path)]}))
        await core.build()
        self.assertIn("music.hello", core.manager.capabilities)
        self.assertEqual((await core.manager.capabilities["music.hello"].run({}, None)).summary, "hi from helper")
        close(core)

    async def test_invalid_extensions_are_rejected_with_clear_errors(self):
        cases = {
            "no_entry": None,
            "no_decl": "x = 1\n",
            "future": "from contracts import Contributions, Extension\nEXTENSION = Extension('future', lambda c: Contributions(), contracts='2')\n",
            "bad_setup": "from contracts import Extension\nEXTENSION = Extension('bad_setup', lambda c: [])\n",
        }
        for name, body in cases.items():
            directory = self.tmp / "bad" / name
            directory.mkdir(parents=True)
            if body is not None:
                (directory / "extension.py").write_text(body)
            with self.subTest(name=name):
                core = new_core(make_config(self.tmp / name, {"extensions": [str(directory)]}))
                with self.assertRaises(ExtensionError):
                    await core.build()
                close(core)

    async def test_capabilities_must_use_their_extensions_namespace(self):
        path = self.extension("thief", """
            from contracts import Capability, Contributions, Extension, ToolResult
            async def run(a, c): return ToolResult.ok("x")
            EXTENSION = Extension("thief", lambda ctx: Contributions(capabilities=[Capability("tools.steal", "x", run)]))
            """)
        core = new_core(make_config(self.tmp, {"extensions": [str(path)]}))
        with self.assertRaisesRegex(ValueError, "namespace"):
            await core.build()
        close(core)

    async def test_assignments_are_checked_against_registered_engines_and_personas(self):
        core = new_core(make_config(self.tmp, {"assignments": {"*": {"engine": "missing", "personas": ["ghost"]}}}))
        with self.assertRaises(ConfigError) as raised:
            await core.build()
        self.assertIn("engine 'missing'", str(raised.exception))
        self.assertIn("unknown personas ['ghost']", str(raised.exception))
        close(core)


class ContractAndStorageTests(unittest.TestCase):
    def test_contract_validation(self):
        with self.assertRaises(ValueError):
            Capability("NoNamespace", "x", None)
        with self.assertRaises(ValueError):
            Capability("a.b", "x", None, {"x": {"type": "string"}}, ("y",))
        with self.assertRaises(ValueError):
            Reply(Address("cli", "x"), "file")
        self.assertEqual(Address.parse("wechat:name:group"), Address("wechat", "name:group"))
        result = ToolResult.ok("done", files=[FileRef("/tmp/a")], data={"n": 1})
        self.assertEqual(ToolResult.from_dict(result.to_dict()), result)
        self.assertEqual(Capability("a.b", "x", None, risk="write").risk, Risk.WRITE)

    def test_tank_and_bottle_are_separate_files_with_isolated_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            tank, bottle = Tank(Path(directory) / "tank.db"), Bottle(Path(directory) / "gateway.db")
            tank.store("extension:a").set("k", 1)
            bottle.interface_store("wechat").set("k", 2)
            self.assertIsNone(tank.store("extension:b").get("k"))
            self.assertIsNone(bottle.interface_store("discord").get("k"))
            self.assertEqual(bottle.interface_store("wechat").get("k"), 2)
            tank.create_run(RunRecord("r", "cli:x", "cli:x", "e", [], {}, "waiting_approval", "person", {}))
            tank.create_approval("ab12", "r", "call:1", "t.x", {}, "cli:x")
            self.assertTrue(tank.decide_approval("ab12", "approved", "owner"))
            self.assertFalse(tank.decide_approval("ab12", "denied", "owner"))
            tank.close()
            bottle.close()


if __name__ == "__main__":
    unittest.main()

"""Setting up: module settings (contract), file edits, the guided setup, `$setup`, the console as creator."""

import asyncio
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from contracts import Address, Setting
from core.config import CoreConfig
from core.loader import import_extension
from core.setup import Setup, parse, set_env, set_key
from core.wizard import Wizard
from tests.core_support import FIXTURES, ROOT, iface, settle, start_core

MODULE = '''
from contracts import Contributions, Extension, Setting

async def check(token):
    if token == "bad":
        raise ValueError("rejected")
    return "logs in as Tester"

EXTENSION = Extension("{name}", lambda context: Contributions(), contracts="1", description="{name} module", settings=(
    Setting("token", "Its token", "secret", env="SETUPTEST_{upper}_TOKEN", ask=True, check=check),
    Setting("colour", "A colour", "text", "green"),
    Setting("channels", "Channel IDs", "list", []),
    Setting("fast", "Go fast", "boolean", False),
))
'''


def modules(root: Path, *names: str) -> Path:
    folder = root / "mods"
    for name in names:
        (folder / name).mkdir(parents=True)
        (folder / name / "extension.py").write_text(MODULE.format(name=name, upper=name.upper()))
    return folder


class ContractTests(unittest.TestCase):
    def test_settings_are_checked_when_declared(self):
        with self.assertRaisesRegex(ValueError, "needs `env`"):
            Setting("token", "x", "secret")
        with self.assertRaisesRegex(ValueError, "kind"):
            Setting("x", "x", "colour")
        with self.assertRaisesRegex(ValueError, "lowercase"):
            Setting("Bad-Key", "x")
        from contracts import Contributions, Extension
        with self.assertRaisesRegex(ValueError, "unique"):
            Extension("x", lambda c: Contributions(), settings=(Setting("a", "x"), Setting("a", "y")))
        self.assertEqual(Setting("t", "x", "secret", env="ONE").env, ("ONE",))

    def test_every_included_module_declares_valid_settings(self):
        for folder in [*sorted((ROOT / "extensions").iterdir()), *sorted((ROOT / "plugins").iterdir())]:
            if (folder / "extension.py").is_file():
                with self.subTest(module=folder.name):
                    extension = import_extension(folder)[0]
                    self.assertEqual(len({s.key for s in extension.settings}), len(extension.settings))
        discord = import_extension(ROOT / "extensions" / "discord")[0]
        self.assertTrue(discord.settings[0].ask and discord.settings[0].kind == "secret")


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_one_key_changes_and_everything_else_stays_byte_for_byte(self):
        path = self.tmp / "core.yml"
        original = textwrap.dedent("""\
            # my settings
            owners:
              discord: ['1']   # me
            personas:
              whale:
                prompt: >-
                  a long folded
                  prompt
            """)
        path.write_text(original)
        set_key(path, "modules", {"exclude": ["extensions/terminal"]})
        text = path.read_text()
        self.assertTrue(text.startswith("# my settings\nmodules:\n  exclude:\n  - extensions/terminal\nowners:"))
        self.assertIn("  discord: ['1']   # me\n", text)
        self.assertIn("      a long folded\n      prompt\n", text)
        set_key(path, "modules", None)
        self.assertEqual(path.read_text(), original)
        set_key(path, "owners", {"discord": ["2"]})
        self.assertEqual(yaml.safe_load(path.read_text())["owners"], {"discord": ["2"]})
        (self.tmp / "list.yml").write_text("- a\n")
        with self.assertRaisesRegex(ValueError, "not a YAML mapping"):
            set_key(self.tmp / "list.yml", "x", 1)

    def test_top_level_lists_written_at_column_zero_can_be_changed_and_reset(self):
        path = self.tmp / "config.yml"
        path.write_text("# discord\nthread_channels:\n- '1'\n- '2'\nmath: true\n")
        set_key(path, "thread_channels", ["3"])
        self.assertEqual(yaml.safe_load(path.read_text()), {"thread_channels": ["3"], "math": True})
        set_key(path, "thread_channels", None)
        self.assertEqual(path.read_text(), "# discord\nmath: true\n")

    def test_env_values_replace_in_place_and_the_file_is_private(self):
        path = self.tmp / ".env"
        path.write_text("# keys\nexport A=old\nB=keep\nA=duplicate\n")
        with mock.patch.dict(os.environ):
            set_env(path, "A", "new")
            set_env(path, "C", "has space")
            self.assertEqual(os.environ["A"], "new")
        self.assertEqual(path.read_text(), '# keys\nA=new\nB=keep\nC="has space"\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            set_env(path, "D", "two\nlines")

    def test_answers_are_parsed_by_kind(self):
        self.assertEqual(parse(Setting("x", "", "list"), "<#123>, 456  789"), ["123", "456", "789"])
        self.assertEqual(parse(Setting("x", "", "list"), "none"), [])
        self.assertEqual(parse(Setting("x", "", "number"), "2.5"), 2.5)
        self.assertEqual(parse(Setting("x", "", "number"), "3"), 3)
        self.assertIs(parse(Setting("x", "", "boolean"), "Off"), False)
        for kind, text in (("number", "lots"), ("boolean", "maybe")):
            with self.assertRaises(ValueError):
                parse(Setting("x", "", kind), text)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mods = modules(self.tmp, "alpha", "beta")
        self.file = self.tmp / "core.yml"
        self.file.write_text(f"env_files: [.env]\nmodules: {{dirs: [{self.mods}]}}\n"
                             "extension_config: {beta: {colour: red}}\n")
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self._tmp.cleanup()

    def test_values_come_from_core_yml_config_yml_env_or_the_default(self):
        setup = Setup(CoreConfig.load(self.file))
        alpha, beta = setup.module("alpha"), setup.module("beta")
        self.assertEqual(setup.value(alpha, alpha.setting("colour")), ("green", "default"))
        self.assertEqual(setup.value(beta, beta.setting("colour")), ("red", "core.yml"))
        self.assertEqual([item.key for item in setup.missing(alpha)], ["token"])
        setup.write(alpha, alpha.setting("colour"), "blue")
        setup.write(alpha, alpha.setting("token"), "secret-value")
        self.assertEqual(setup.value(alpha, alpha.setting("colour")), ("blue", "config.yml"))
        self.assertEqual(setup.value(alpha, alpha.setting("token")), (True, ".env"))
        self.assertNotIn("secret-value", (self.mods / "alpha" / "config.yml").read_text())
        self.assertIn("SETUPTEST_ALPHA_TOKEN=secret-value", (self.tmp / ".env").read_text())
        setup.write(alpha, alpha.setting("colour"), None)
        self.assertEqual(setup.value(alpha, alpha.setting("colour")), ("green", "default"))

    def test_switched_off_modules_are_never_imported(self):
        (self.mods / "gamma").mkdir()
        (self.mods / "gamma" / "extension.py").write_text("raise SystemExit('imported!')\n")
        self.file.write_text(self.file.read_text().replace("modules: {", "modules: {exclude: [gamma], "))
        setup = Setup(CoreConfig.load(self.file))
        gamma = setup.module("gamma")
        self.assertTrue(gamma.excluded)
        self.assertEqual((gamma.extension, gamma.settings), (None, ()))
        self.assertIn("not loaded", gamma.description)

    def test_switching_modules_writes_only_the_exclude_list(self):
        setup = Setup(CoreConfig.load(self.file))
        setup.enable(setup.module("beta"), False)
        self.assertEqual(yaml.safe_load(self.file.read_text())["modules"]["exclude"], ["beta"])
        config = CoreConfig.load(self.file)
        self.assertEqual([path.name for path in config.modules()], ["alpha"])
        setup = Setup(config)
        self.assertTrue(setup.module("beta").excluded)
        setup.enable(setup.module("beta"), True)
        self.assertNotIn("exclude", yaml.safe_load(self.file.read_text())["modules"])


class WizardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mods = modules(self.tmp, "alpha", "beta")
        self.file = self.tmp / "core.yml"
        self.file.write_text(f"env_files: [.env]\ndata_dir: data\nmodules: {{dirs: [{self.mods}], "
                             f"paths: [{ROOT / 'extensions' / 'assistant'}]}}\n")
        self.environment = mock.patch.dict(os.environ, {name: "" for name in (
            "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "CLAUDE_API_KEY", "OPENROUTER_API_KEY")})
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop()
        self._tmp.cleanup()

    async def run_wizard(self, answers, secrets):
        answers, secrets, said, tested = iter(answers), iter(secrets), [], []

        async def test_key(provider, key):
            tested.append((provider, key))
            return (key == "good", "answered" if key == "good" else "401 unauthorized")
        def typed(source):
            def read(prompt):
                value = next(source, None)
                if value is None:
                    raise EOFError  # what input() does when the terminal is closed
                return value
            return read
        wizard = Wizard(self.file, ask=typed(answers), secret=typed(secrets), say=said.append, test_key=test_key)
        code = await wizard.run()
        return code, "\n".join(said), tested

    async def test_a_fresh_install_is_set_up_from_the_console(self):
        code, said, tested = await self.run_wizard(
            answers=["openai", "n", "deepseek", "",                    # keys: a bad one refused, a good one
                     "beta",                                             # switch beta off
                     "n",                                                # don't save the rejected token
                     "alpha", "colour", "blue", "alpha", "channels", "<#5>, 6", "", ""],
            secrets=["bad-key", "good",                                 # the keys
                     "bad", "tok"])                                      # alpha's token: rejected, then good
        self.assertEqual(code, 0, said)
        self.assertEqual(tested, [("openai", "bad-key"), ("deepseek", "good")])
        env = (self.tmp / ".env").read_text()
        self.assertIn("DEEPSEEK_API_KEY=good", env)
        self.assertNotIn("bad-key", env)
        self.assertIn("SETUPTEST_ALPHA_TOKEN=tok", env)
        self.assertEqual(yaml.safe_load((self.mods / "alpha" / "config.yml").read_text()),
                         {"colour": "blue", "channels": ["5", "6"]})
        self.assertEqual(yaml.safe_load(self.file.read_text())["modules"]["exclude"], ["beta"])
        self.assertIn("✗ rejected", said)
        self.assertIn("✓ logs in as Tester", said)
        self.assertIn("✓ modules: alpha", said)
        self.assertIn("✓ model providers with keys: deepseek", said)

    async def test_a_truncated_answer_still_proves_the_key_works(self):
        wizard = Wizard(self.file, ask=lambda prompt: "", secret=lambda prompt: "", say=lambda text: None)
        from core.providers import registry
        class Truncating:
            def __init__(self, *args, **kwargs):
                pass
            async def generate(self, request, profile):
                assert request.max_tokens >= 1000
                raise RuntimeError("The model output was truncated")
        with mock.patch.object(registry, "Providers", Truncating):
            ok, note = await wizard._test_key("deepseek", "k")
        self.assertTrue(ok, note)

    async def test_stopping_halfway_keeps_what_was_saved(self):
        code, said, _ = await self.run_wizard(answers=["deepseek"], secrets=["good"])
        self.assertEqual(code, 1)
        self.assertIn("Stopped. What was saved stays.", said)
        self.assertIn("DEEPSEEK_API_KEY=good", (self.tmp / ".env").read_text())


class ChatSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        mods = modules(self.tmp, "alpha")
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.core = await start_core(self.tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                                                               str(mods / "alpha")]})
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self.environment.stop()
        self._tmp.cleanup()

    async def say(self, text, sender="boss"):
        await self.chat.say(text, sender=sender)
        await settle(self.core)
        return self.chat.delivered[-1]

    async def test_the_creator_changes_settings_in_a_chat_but_never_secrets(self):
        self.assertEqual((await self.say("$setup", sender="alice")).code, "not_permitted")
        listing = (await self.say("$setup")).text
        self.assertIn("alpha · external · alpha module · needs token", listing)
        self.assertIn("token = not set", (await self.say("$setup alpha")).text)
        refused = await self.say("$setup alpha token pasted-secret")
        self.assertIn("is a secret", refused.text)
        self.assertTrue(refused.private)
        saved = (await self.say("$setup alpha channels 7, <#8>")).text
        self.assertIn("alpha.channels = 7, 8", saved)
        self.assertIn("Restart the bot", saved)
        self.assertEqual(yaml.safe_load((self.tmp / "mods" / "alpha" / "config.yml").read_text()),
                         {"channels": ["7", "8"]})
        self.assertIn("Not saved: Answer yes or no", (await self.say("$setup alpha fast perhaps")).text)
        self.assertIn("switched off", (await self.say("$setup off alpha")).text)
        self.assertIn("is off, so it is not loaded", (await self.say("$setup alpha")).text)
        self.assertEqual(yaml.safe_load((self.tmp / "core.yml").read_text()), {"modules": {"exclude": ["alpha"]}})
        history = [item.text for item in self.core.tank.recent_messages("chat:room", 50)]
        self.assertFalse(any("$setup" in text or "pasted-secret" in text for text in history))


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_console_is_the_creator_and_approvals_go_there_only_as_a_fallback(self):
        cli = import_extension(ROOT / "extensions" / "cli")[1]
        reported = []
        port = SimpleNamespace(set_owners=lambda ids, control=None, fallback=False: reported.append(
            (ids, str(control), fallback)), submit=None, log=lambda *a, **k: None)
        tty = SimpleNamespace(isatty=lambda: True, readline=lambda: "")
        out = SimpleNamespace(lines=[], write=lambda text: out.lines.append(text), flush=lambda: None)
        interface = cli.CLIInterface(stdin=tty, stdout=out)
        await interface.start(port)
        await interface.stop()
        self.assertEqual(reported, [(["local"], "cli:local", True)])
        self.assertIn("You are the creator", out.lines[0])
        piped = cli.CLIInterface(stdin=SimpleNamespace(isatty=lambda: False, readline=lambda: ""), stdout=out)
        reported.clear()
        await piped.start(port)
        await piped.stop()
        self.assertEqual(reported, [])

    async def test_a_fallback_control_never_replaces_a_real_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = await start_core(Path(tmp), {"control": None})
            manager = core.manager
            manager.report_owners("cli", ["local"], Address("cli", "local"), fallback=True)
            self.assertEqual(str(manager.control), "cli:local")
            manager.report_owners("discord", ["42"], Address("discord", "dm:42"))
            manager.report_owners("cli", ["local"], Address("cli", "local"), fallback=True)
            self.assertEqual(str(manager.control), "discord:dm:42")
            await core.stop()


if __name__ == "__main__":
    unittest.main()

"""Terminal extension: a creator-only shell per conversation, through `$t` or a terminal conversation."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

from contracts import Address, Sender
from core.loader import import_extension
from core.manager.routines import Caller
from tests.core_support import FIXTURES, ROOT, iface, settle, start_core

FAST = {"quiet_s": 0.4, "max_wait_s": 5, "update_s": 0.2}


def shell_module():
    _, module = import_extension(ROOT / "extensions" / "terminal")
    return __import__(module.__name__.rsplit(".", 1)[0] + ".shell", fromlist=["Terminals"])


@unittest.skipIf(os.name == "nt", "these shell lines are POSIX")
class ShellTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.terminals = shell_module().Terminals(cwd=Path(tempfile.gettempdir()), poll_s=0.05, **FAST)
        self.progress = []

    async def asyncTearDown(self):
        for shell in self.terminals.shells.values():
            await shell.close()

    async def say(self, text, key="a"):
        async def progress(text):
            self.progress.append(text)
        return await self.terminals.exchange(key, text, progress, markdown=False)

    async def test_each_conversation_keeps_its_own_shell(self):
        await self.say("cd / && X=5")
        self.assertEqual(await self.say("pwd; echo $X"), "/\n5")
        self.assertEqual(await self.say("echo [$X]", key="b"), "[]")

    async def test_output_is_shown_while_it_runs_and_interrupting_keeps_the_shell(self):
        output = await self.say("for i in 1 2 3 4; do echo line$i; sleep 0.15; done")
        self.assertEqual(output, "line1\nline2\nline3\nline4")
        self.assertTrue(self.progress and self.progress[0].startswith("running…\nline1"))
        output = await self.say("sleep 30")
        self.assertIn("(still running: sleep;", output)
        await self.say("^C")
        self.assertEqual(self.terminals.shells["a"].running(), [])
        self.assertEqual(await self.say("echo alive"), "alive")

    async def test_ctrl_c_typed_meanwhile_stops_a_loop_of_the_shell_itself(self):
        typed = iter([[], [], ["^C"]])

        async def progress(text):
            pass
        output = await self.terminals.exchange("a", "while true; do echo tick; sleep 0.1; done", progress,
                                               lambda: next(typed, []), markdown=False)
        self.assertTrue(output.startswith("tick"))
        self.assertEqual(self.terminals.shells["a"].running(), [])
        self.assertEqual(await self.say("echo alive"), "alive")

    async def test_later_output_is_shown_by_looking_again(self):
        await self.say("(sleep 0.6; echo late) &")
        await asyncio.sleep(1.0)
        self.assertEqual(await self.say(""), "late")
        self.assertEqual(await self.say(""), "(no new output)")

    async def test_an_exited_shell_is_replaced_and_says_so(self):
        self.assertIn("(shell exited with code 3)", await self.say("exit 3"))
        output = await self.say("echo again")
        self.assertEqual(output, "(the previous shell exited with code 3; this is a new one)\nagain")

    def test_long_output_keeps_its_end_and_progress_bars_their_last_state(self):
        render = shell_module().Terminals.render
        self.assertEqual(render("a" * 50 + "END", 10, False), "…" + "a" * 7 + "END")
        self.assertEqual(render("10%\r50%\r100%\ndone", 100, True), "```\n100%\ndone\n```")
        self.assertEqual(render("50%\r", 100, False) + "|" + render("half\r\nline", 100, False), "50%|half\nline")
        started = time.monotonic()
        render("x" * 200_000 + "\r" + "y" * 10, 1800, True)  # one huge line must not stall the bot
        self.assertLess(time.monotonic() - started, 0.5)

    async def test_simultaneous_first_lines_share_one_shell_and_a_failed_start_is_retried(self):
        outputs = await asyncio.gather(self.say("echo one"), self.say("echo two"))
        self.assertEqual(len(self.terminals.shells), 1)
        self.assertTrue(all(isinstance(output, str) for output in outputs))
        broken = shell_module().Terminals(cwd=Path(tempfile.gettempdir()) / "missing-dir", poll_s=0.05, **FAST)
        for _ in range(2):
            with self.assertRaises(FileNotFoundError):
                await broken.exchange("k", "echo hi", self.say)
        self.assertEqual(broken.shells, {})


@unittest.skipIf(os.name == "nt", "these shell lines are POSIX")
class TerminalCoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = await start_core(Path(self._tmp.name), {
            "extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"), str(ROOT / "extensions" / "terminal")],
            "extension_config": {"terminal": {**FAST, "cwd": self._tmp.name}},
            "assignments": {"chat:term": {"engine": "terminal", "persona": "terminal"}}})
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        for shell in self.core.manager.engines["terminal"].terminals.shells.values():
            await shell.close()
        self._tmp.cleanup()

    async def test_only_the_creator_reaches_the_shell(self):
        await self.chat.say("$t echo from-command", sender="boss")
        await self.chat.say("$t echo nope")
        await self.chat.say("echo nope", conversation="term")
        await settle(self.core)
        self.assertEqual(self.chat.texts("room", "text"), ["from-command"])
        self.assertEqual([reply.code for reply in self.chat.delivered if reply.address.conversation == "room"
                          and reply.kind == "notice"], ["not_permitted"])
        self.assertEqual(self.chat.texts("term"), ["Only the bot's creator can use the terminal."])

    async def test_only_routines_a_person_typed_reach_a_terminal_conversation(self):
        creator = Caller(Address("chat", "term"), Sender("boss", "Boss"), True, "creator")
        self.core.manager.routines.add(creator, "every 30m", "echo from-a-model", via="tool")
        self.core.manager.routines.add(creator, "every 30m", "echo typed", via="command")
        await self.core.gateway.tick(time.time() + 3600)
        await settle(self.core)
        texts = self.chat.texts("term", "text")
        self.assertIn("typed", texts)
        self.assertIn("A routine set up through a tool cannot type into the terminal; type `$routine` yourself "
                      "for that.", texts)
        self.assertFalse(any("from-a-model" in text for text in texts))

    async def test_models_reach_the_shell_only_for_the_creator_and_each_line_is_approved(self):
        await self.chat.say('tool terminal.send {"input": "echo model-line"}', sender="boss")
        await settle(self.core)
        approval = self.core.tank.approvals("pending")[0]
        self.assertEqual((approval["name"], approval["arguments"]), ("terminal.send", {"input": "echo model-line"}))
        await iface(self.core, "owner").say(f"$approve {approval['id']}", conversation="dm", sender="boss")
        await settle(self.core)
        self.assertIn("approved: model-line", self.chat.texts("room", "text"))
        await self.chat.say('tool terminal.peek {}', sender="boss")
        await self.chat.say('tool terminal.send {"input": "echo nope"}')
        await settle(self.core)
        texts = self.chat.texts("room", "text")
        self.assertIn("success: (no new output)", texts)
        self.assertIn("failed: terminal.send is available only to the owner", texts)

    async def test_a_terminal_conversation_and_t_share_one_shell(self):
        await self.chat.say("Y=7", conversation="term", sender="boss")
        await settle(self.core)
        await self.chat.say("$t echo $Y", conversation="term", sender="boss")
        await settle(self.core)
        self.assertEqual(self.chat.texts("term", "text"), ["(no output)", "7"])

    async def test_cancelling_a_t_run_interrupts_the_program(self):
        await self.chat.say("$t while true; do echo tick; sleep 0.1; done", sender="boss")
        for _ in range(100):
            runs = self.core.tank.runs(("running",))
            if runs and self.chat.texts("room", "progress"):
                break
            await asyncio.sleep(0.05)
        await self.chat.say(f"$cancel {runs[0].id}", sender="boss")
        await settle(self.core)
        self.assertEqual(self.core.tank.run(runs[0].id).status, "cancelled")
        self.assertEqual(self.chat.texts("room", "text"), [f"Cancelled run {runs[0].id}."])
        shell = self.core.manager.engines["terminal"].terminals.shells["chat:room"]
        for _ in range(200):  # the interrupted program may take a moment to exit (longer on a busy machine)
            if not shell.running():
                break
            await asyncio.sleep(0.05)
        self.assertEqual(shell.running(), [])

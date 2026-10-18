"""The supervisor (parent) and the core's side of it: restarts, crashes, hangs, giving up, reports."""

import asyncio
import json
import os
import signal
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from core import supervised
from core.supervisor import RESTART, Supervisor
from tests.core_support import iface, settle, start_core

# One fake core per run: it logs the reason it was handed, then behaves as the plan says for that run.
CHILD = textwrap.dedent("""
    import os, sys, time, pathlib
    log, plan = pathlib.Path(sys.argv[1]), sys.argv[2].split(",")
    runs = log.read_text().splitlines() if log.exists() else []
    reason = pathlib.Path(os.environ["CORE_RESTART_REASON"])
    with log.open("a") as out:
        out.write((reason.read_text() if reason.exists() else "none") + "\\n")
    reason.unlink(missing_ok=True)
    step = plan[min(len(runs), len(plan) - 1)]
    beat = pathlib.Path(os.environ["CORE_HEARTBEAT"])
    if step == "restart":
        sys.exit(75)
    if step == "crash":
        print("Traceback (most recent call last):\\nValueError: boom", file=sys.stderr)
        sys.exit(3)
    if step == "hang":
        beat.write_text("x")
        time.sleep(60)
    if step == "wait":
        count = 0
        while True:
            count += 1
            beat.write_text(str(count))
            time.sleep(0.05)
    sys.exit(0)
""")


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "child.py").write_text(CHILD)
        self.log = self.tmp / "log"

    def tearDown(self):
        self._tmp.cleanup()

    def supervisor(self, plan, **options):
        options = {"hang_s": 0.6, "grace_s": 5, "backoff": (0.05,), "poll_s": 0.05, "stop_grace_s": 2, **options}
        supervisor = Supervisor([sys.executable, str(self.tmp / "child.py"), str(self.log), plan], **options)
        supervisor._install = lambda: None  # signal handlers belong to the test runner
        return supervisor

    def reasons(self):
        return [None if line == "none" else json.loads(line) for line in self.log.read_text().splitlines()]

    def test_a_restart_a_crash_and_a_hang_each_start_a_fresh_core_that_is_told_why(self):
        self.assertEqual(self.supervisor("restart,crash,hang,exit").run(), 0)
        first, requested, crashed, hung = self.reasons()
        self.assertIsNone(first)
        self.assertEqual(requested["reason"], "requested")
        self.assertEqual((crashed["reason"], crashed["code"], crashed["restarts"]), ("crash", 3, 1))
        self.assertEqual(crashed["detail"][-1], "ValueError: boom")
        self.assertEqual((hung["reason"], hung["restarts"]), ("hang", 2))

    def test_a_core_that_keeps_crashing_is_given_up_on(self):
        self.assertEqual(self.supervisor("crash", max_crashes=3).run(), 1)
        self.assertEqual(len(self.reasons()), 3)

    def test_a_core_that_never_beats_is_hung_after_the_start_grace(self):
        (self.tmp / "child.py").write_text(CHILD.replace('beat.write_text("x")\n    time.sleep(60)',
                                                          'time.sleep(60)'))
        started = time.monotonic()
        self.assertEqual(self.supervisor("hang,exit", grace_s=0.8).run(), 0)
        self.assertEqual(self.reasons()[1]["reason"], "hang")
        self.assertLess(time.monotonic() - started, 20)

    def test_stopping_the_supervisor_stops_the_core_and_does_not_restart_it(self):
        supervisor = self.supervisor("wait")
        threading.Timer(1.0, supervisor._stop, args=(getattr(signal, "SIGTERM", 15), None)).start()
        self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(self.reasons()), 1)
        self.assertFalse(supervisor.folder.exists())

    def test_exit_codes(self):
        self.assertEqual(RESTART, 75)

    def test_a_wall_clock_jump_does_not_make_a_healthy_core_look_hung(self):
        real, jumps = time.time, iter(range(0, 10**9, 10_000))
        supervisor = self.supervisor("wait")
        threading.Timer(1.5, supervisor._stop, args=(getattr(signal, "SIGTERM", 15), None)).start()
        with mock.patch("core.supervisor.time.time", lambda: real() + next(jumps)):
            self.assertEqual(supervisor.run(), 0)
        self.assertEqual(len(self.reasons()), 1)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "POSIX only")
    def test_a_signal_nohup_ignores_stays_ignored(self):
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            Supervisor(["true"])._install()
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, previous)
            for name in ("SIGTERM", "SIGINT"):
                signal.signal(getattr(signal, name), signal.default_int_handler if name == "SIGINT"
                              else signal.SIG_DFL)


class ReportTests(unittest.TestCase):
    def test_reasons_read_plainly(self):
        now = 1_000_000.0
        self.assertEqual(supervised.describe({"reason": "requested", "by": "Leo", "at": now - 6}, now),
                         "🔄 Restarted as Leo asked (down 6 s).")
        crash = supervised.describe({"reason": "crash", "code": 1, "at": now - 14, "restarts": 2,
                                     "detail": ["  File x", "ValueError: boom"]}, now)
        self.assertIn("after a crash at", crash)
        self.assertIn("exit code 1, down 14 s", crash)
        self.assertIn("ValueError: boom", crash)
        self.assertIn("Restart 2 in the last 10 minutes.", crash)
        self.assertIn("stopped responding", supervised.describe({"reason": "hang", "at": now - 300}, now))
        self.assertIn("down 5 min", supervised.describe({"reason": "hang", "at": now - 300}, now))


class CoreSideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = await start_core(self.tmp)
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def test_the_new_core_tells_the_control_route_why_it_restarted(self):
        reason = self.tmp / "reason.json"
        reason.write_text(json.dumps({"reason": "crash", "code": 1, "at": time.time() - 3, "detail": ["Boom"]}))
        with mock.patch.dict(os.environ, {"CORE_RESTART_REASON": str(reason)}):
            await supervised.report(self.core, wait_s=5)
        notice = iface(self.core, "owner").notices("restarted")[0]
        self.assertEqual(notice.address.conversation, "dm")
        self.assertIn("after a crash", notice.text)
        self.assertFalse(reason.exists())

    async def test_only_the_creator_restarts_and_only_under_the_supervisor(self):
        await self.chat.say("$admin restart", sender="boss")
        await settle(self.core)
        self.assertIn("cannot restart itself", self.chat.texts("room")[-1])
        asked = []
        self.core.manager.restart = asked.append
        await self.chat.say("$admin restart", sender="alice")
        await settle(self.core)
        self.assertEqual(self.chat.texts("room")[-1], "Only the creator can restart the bot.")
        await self.chat.say("$admin restart", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.chat.texts("room")[-1], "🔄 Restarting; I'll say when I'm back.")
        await asyncio.sleep(1.2)
        self.assertEqual(asked, ["Boss"])

    async def test_setup_changes_offer_a_restart_button_when_supervised(self):
        self.core.manager.restart = lambda by: None
        await self.chat.say("$setup on probe", sender="boss")
        await settle(self.core)
        reply = self.chat.delivered[-1]
        self.assertEqual(reply.actions, ())  # nothing changed: probe is not off
        await self.chat.say("$setup off probe", sender="boss")
        await settle(self.core)
        self.assertEqual([action.command for action in self.chat.delivered[-1].actions], ["$admin restart"])

    async def test_a_restart_request_ends_run_forever_with_exit_code_75(self):
        self.core.request_stop(RESTART)
        await asyncio.wait_for(self.core.run_forever(), 5)
        self.assertEqual(self.core.exit_code, 75)


class SignalTests(unittest.TestCase):
    def test_only_the_first_stop_signal_counts(self):
        from types import SimpleNamespace
        stops, cancels = [], []
        core = SimpleNamespace(started=True, request_stop=stops.append)
        handle = supervised.stop_on_signal(core, SimpleNamespace(cancel=lambda: cancels.append(1)))
        handle(signal.SIGTERM)
        handle(signal.SIGTERM)
        self.assertEqual((stops, cancels), ([0], []))
        handle(signal.SIGINT)  # a second Ctrl-C insists
        self.assertEqual((stops, cancels), ([0], [1]))
        cancels.clear()
        starting = SimpleNamespace(started=False, request_stop=stops.append)
        supervised.stop_on_signal(starting, SimpleNamespace(cancel=lambda: cancels.append(1)))()
        self.assertEqual(cancels, [1])


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_it_beats_and_stops_the_core_when_the_supervisor_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            beat = Path(tmp) / "heartbeat"
            stopped = []
            with mock.patch.dict(os.environ, {"CORE_HEARTBEAT": str(beat), "CORE_SUPERVISOR_PID": str(os.getppid())}):
                task = asyncio.create_task(supervised.heartbeat(lambda: stopped.append(1)))
                await asyncio.sleep(0.1)
                self.assertTrue(beat.is_file())
                task.cancel()
            with mock.patch.dict(os.environ, {"CORE_HEARTBEAT": str(beat), "CORE_SUPERVISOR_PID": "999999999"}):
                await asyncio.wait_for(supervised.heartbeat(lambda: stopped.append(1)), 2)
            self.assertEqual(stopped, [1])


if __name__ == "__main__":
    unittest.main()

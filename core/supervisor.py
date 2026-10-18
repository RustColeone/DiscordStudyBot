"""The supervisor: `python -m core` runs this small parent, which runs the real core as its child.

Like a hardware watchdog, something very simple watches the complicated thing. The child is
`python -m core serve`; it touches a heartbeat file every few seconds and shares this console
(stdin and stdout are passed through, so the CLI works as before). Its stderr is copied to ours and
the last lines are kept, so a crash can be explained.

When the child exits:
* 0: stop (Ctrl-C, `systemctl stop`, `--idle-exit`); the supervisor exits too.
* 75 (RESTART): it asked to be restarted (`$admin restart`); start a fresh one at once.
* anything else: a crash; start a fresh one after 1, 5, 30, then 60 s, and give up after
  `max_crashes` crashes within `window_s` rather than loop forever.
A child whose heartbeat stops for `hang_s` (or that never beats within `grace_s` of starting) is hung:
it is killed with everything it started, and restarted like a crash.

Before starting the next child the supervisor writes why (`reason.json`: requested, crash or hang,
when, the exit code, the end of the error) for it to report to the owner once it is connected. The
supervisor itself never talks to any platform and imports nothing from the core.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Sequence

RESTART = 75
BACKOFF = (1, 5, 30, 60)


def say(text: str) -> None:
    print(f"[supervisor] {text}", file=sys.stderr, flush=True)


def kill_tree(process: subprocess.Popen, grace_s: float) -> None:
    """Ask the child to stop, then end it and everything it started (shells, ffmpeg) if it does not."""
    try:
        import psutil
        descendants = psutil.Process(process.pid).children(recursive=True)
    except Exception:
        descendants = []
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(grace_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    for child in descendants:
        try:
            child.kill()
        except Exception:
            pass


class Supervisor:
    def __init__(self, command: Sequence[str], *, hang_s: float = 60.0, grace_s: float = 180.0,
                 backoff: Sequence[float] = BACKOFF, max_crashes: int = 5, window_s: float = 600.0,
                 poll_s: float = 1.0, stop_grace_s: float = 20.0):
        self.command = list(command)
        self.hang_s, self.grace_s, self.backoff = hang_s, grace_s, tuple(backoff)
        self.max_crashes, self.window_s, self.poll_s, self.stop_grace_s = max_crashes, window_s, poll_s, stop_grace_s
        self.folder: Path | None = None  # made by run() and removed when it ends
        self.stopping = False
        self.child: subprocess.Popen | None = None

    # ---- stopping on request ----------------------------------------------------------------------
    def _stop(self, signum=None, frame=None) -> None:
        self.stopping = True
        if self.child is not None and self.child.poll() is None and signum != getattr(signal, "SIGINT", None):
            # Ctrl-C already reached the child through the console; other signals are passed on.
            try:
                self.child.terminate()
            except OSError:
                pass

    def _install(self) -> None:
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            if not hasattr(signal, name):
                continue
            number = getattr(signal, name)
            if signal.getsignal(number) is signal.SIG_IGN:
                continue  # `nohup` (or a shell) asked to ignore it: so do we, and so does the core after us
            try:
                signal.signal(number, self._stop)
            except (ValueError, OSError):
                pass

    # ---- one child ----------------------------------------------------------------------------------
    def _spawn(self) -> tuple[subprocess.Popen, deque, threading.Thread]:
        self.heartbeat.unlink(missing_ok=True)
        environment = {**os.environ, "CORE_HEARTBEAT": str(self.heartbeat), "CORE_RESTART_REASON": str(self.reason),
                       "CORE_SUPERVISOR_PID": str(os.getpid())}
        child = subprocess.Popen(self.command, stderr=subprocess.PIPE, env=environment)
        tail: deque[str] = deque(maxlen=40)

        def copy():
            for line in child.stderr:
                try:
                    sys.stderr.buffer.write(line)
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass
                tail.append(line.decode("utf-8", "replace").rstrip())
        reader = threading.Thread(target=copy, name="supervisor-stderr", daemon=True)
        reader.start()
        return child, tail, reader

    def _watch(self, child: subprocess.Popen) -> str | None:
        """Wait for the child to exit. Returns "hang" if it stopped beating and was killed.

        Silence is measured on the monotonic clock, from the last time the heartbeat file's content
        changed: the wall clock can jump (a Raspberry Pi setting its time after boot, a laptop waking up)
        and must never make a healthy core look hung."""
        started = time.monotonic()
        seen, beaten = None, None
        while child.poll() is None:
            time.sleep(self.poll_s)
            if self.stopping:
                waited = time.monotonic()
                while child.poll() is None and time.monotonic() - waited < self.stop_grace_s:
                    time.sleep(self.poll_s)
                if child.poll() is None:
                    kill_tree(child, 1.0)
                return None
            try:
                content = self.heartbeat.read_text(encoding="utf-8")
            except OSError:
                content = None
            if content is not None and content != seen:
                seen, beaten = content, time.monotonic()
            silent = time.monotonic() - (beaten if beaten is not None else started)
            if silent > (self.hang_s if beaten is not None else self.grace_s):
                say(f"no heartbeat for {silent:.0f} s; the core is hung, restarting it")
                kill_tree(child, 10.0)
                return "hang"
        return None

    # ---- the loop -------------------------------------------------------------------------------------
    @property
    def heartbeat(self) -> Path:
        return self.folder / "heartbeat"

    @property
    def reason(self) -> Path:
        return self.folder / "reason.json"

    def run(self) -> int:
        self.folder = Path(tempfile.mkdtemp(prefix="core-supervisor-"))
        self._install()
        crashes: deque[float] = deque()
        try:
            while True:
                child, tail, reader = self._spawn()
                self.child = child
                try:
                    hung = self._watch(child)
                except KeyboardInterrupt:  # Windows: Ctrl-C reaches both processes
                    self.stopping = True
                    hung = self._watch(child)
                code = child.wait()
                reader.join(timeout=2)
                child.stderr.close()
                if self.stopping or (code == 0 and not hung):
                    return 0
                if code == RESTART and not hung:
                    if not self.reason.exists():
                        self._write({"reason": "requested"})
                    say("restarting the core as requested")
                    continue
                now = time.time()
                crashes.append(now)
                while crashes and now - crashes[0] > self.window_s:
                    crashes.popleft()
                if len(crashes) >= self.max_crashes:
                    say(f"the core failed {len(crashes)} times within {self.window_s / 60:.0f} minutes; giving up. "
                        "Fix the error above, then start it again.")
                    return 1
                error = [line for line in tail if line.strip()]
                self._write({"reason": hung or "crash", "code": code, "detail": error[-3:] if error else [],
                             "restarts": len(crashes)})
                delay = self.backoff[min(len(crashes), len(self.backoff)) - 1]
                say(f"the core {'hung' if hung else f'exited with code {code}'}; starting it again in {delay:g} s")
                waited = time.time()
                while time.time() - waited < delay:
                    if self.stopping:
                        return 0
                    time.sleep(min(self.poll_s, delay))
        finally:
            if self.child is not None and self.child.poll() is None:
                kill_tree(self.child, self.stop_grace_s)
            shutil.rmtree(self.folder, ignore_errors=True)

    def _write(self, data: dict) -> None:
        data.setdefault("at", time.time())
        self.reason.write_text(json.dumps(data), encoding="utf-8")


def main(arguments: Sequence[str]) -> int:
    return Supervisor([sys.executable, "-m", "core", "serve", *arguments]).run()

"""Shell sessions: one long-lived shell per conversation, driven through plain pipes.

Pipes behave the same on Windows and Linux and need no extra packages. Ordinary commands, scripts and
line-based REPLs work; full-screen programs (vim, top) and password prompts (sudo) do not.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Sequence

import psutil

WINDOWS = os.name == "nt"
NEWLINE = "\r\n" if WINDOWS else "\n"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
KEEP_CHARS = 200_000
INTERRUPT = "^C"
READY = "__terminal_ready__"


def default_shell() -> list[str]:
    if WINDOWS:
        executable = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"]
    bash = shutil.which("bash")
    # Interactive (without rc files or line editing, which would echo input), so Ctrl-C stops a running
    # loop without ending the shell.
    return [bash, "--norc", "--noprofile", "--noediting", "-i"] if bash else ["/bin/sh"]


def setup_lines(argv: Sequence[str]) -> list[str]:
    """Lines that make a fresh shell quiet: no prompts, no history expansion, UTF-8 output."""
    name = Path(argv[0]).name.lower()
    if name.startswith("bash"):
        return ["PS1=''; PS2=''; PROMPT_COMMAND=''; set +H"]
    if "powershell" in name or "pwsh" in name:
        return ["[Console]::OutputEncoding = [System.Text.Encoding]::UTF8"]
    return []


def shell_environment() -> dict[str, str]:
    environment = dict(os.environ)
    # Without a terminal, programs buffer output, page it or colour it; ask them not to.
    environment.update({"PYTHONUNBUFFERED": "1", "PAGER": "cat", "GIT_PAGER": "cat", "NO_COLOR": "1",
                        "TERM": "dumb"})
    return environment


def clean(text: str) -> str:
    """Keep what a carriage return left on each line (progress bars show their latest state)."""
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(next((part for part in reversed(line.split("\r")) if part), "") if "\r" in line else line
                     for line in lines)


class Shell:
    """One shell process. Output is collected continuously; positions in it are absolute character
    counts, so each exchange can show exactly what appeared since it last looked."""

    def __init__(self, argv: Sequence[str], cwd: Path):
        self.argv, self.cwd = list(argv), cwd
        self.process: asyncio.subprocess.Process | None = None
        self.text = ""
        self.total = 0
        self.shown = 0
        self.last_output = 0.0
        self.used = time.monotonic()
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
                   else {"start_new_session": True})
        self.process = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, cwd=str(self.cwd), env=shell_environment(), **options)
        self._reader = asyncio.create_task(self._read(), name=f"terminal-{self.process.pid}")
        for line in setup_lines(self.argv):
            await self.write(line)
        # Skip whatever the shell says on starting (banners, "no job control" notes) up to a marker.
        await self.write(f"echo {READY}")
        for _ in range(100):
            if READY in self.text or not self.alive:
                break
            await asyncio.sleep(0.05)
        self.shown = self.total

    async def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await self.process.stdout.read(4096)
            if not chunk:
                break
            self._append(decoder.decode(chunk))
        self._append(decoder.decode(b"", final=True))

    def _append(self, text: str) -> None:
        text = ANSI.sub("", text).replace("\r\n", "\n")
        if not text:
            return
        self.text = (self.text + text)[-KEEP_CHARS:]
        self.total += len(text)
        self.last_output = time.monotonic()

    def since(self, position: int) -> str:
        count = min(self.total - position, len(self.text))
        return self.text[-count:] if count > 0 else ""

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def write(self, line: str) -> None:
        self.used = time.monotonic()
        try:
            self.process.stdin.write((line + NEWLINE).encode("utf-8"))
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _children(self) -> list[psutil.Process]:
        try:
            return psutil.Process(self.process.pid).children(recursive=True)
        except psutil.Error:
            return []

    def running(self) -> list[str]:
        """Names of the programs the shell is running right now."""
        names = []
        for child in self._children():
            try:
                names.append(child.name())
            except psutil.Error:
                pass
        return names

    def interrupt(self) -> str:
        """Stop what the shell is running, like Ctrl-C. Linux: SIGINT to the shell's process group (an
        interactive bash abandons the running command and stays). Windows, which cannot send Ctrl-C to a
        process without a console: the running programs are ended, and if there were none (a loop in
        the shell itself) the shell is, so a new one starts next time. Returns a note for the reply."""
        if not self.alive:
            return ""
        if not WINDOWS:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
            return ""
        children = self._children()
        for child in children:
            try:
                child.kill()
            except psutil.Error:
                pass
        if children:
            return ""
        self.kill()
        return "(nothing to stop but the shell itself, so it was ended; the next line starts a new one)\n"

    def kill(self) -> None:
        if not self.alive:
            return
        for child in self._children():
            try:
                child.kill()
            except psutil.Error:
                pass
        try:
            self.process.kill()
        except ProcessLookupError:
            pass

    async def close(self) -> None:
        self.kill()
        if self.process is not None:
            await self.process.wait()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)


class Terminals:
    """The shells of all conversations, and the exchange that feeds one input and watches the output."""

    def __init__(self, argv: Sequence[str] | None = None, cwd: Path | None = None, quiet_s: float = 1.5,
                 max_wait_s: float = 20.0, update_s: float = 3.0, idle_s: float = 3600.0, tail_chars: int = 1800,
                 poll_s: float = 0.25):
        self.argv = list(argv or default_shell())
        self.cwd = cwd or Path.home()
        self.quiet_s, self.max_wait_s, self.update_s = quiet_s, max_wait_s, update_s
        self.idle_s, self.tail_chars, self.poll_s = idle_s, tail_chars, poll_s
        self.shells: dict[str, Shell] = {}
        self._starting: dict[str, asyncio.Lock] = {}

    async def shell(self, key: str) -> tuple[Shell, str]:
        """The conversation's shell (started if needed), and a note when an earlier one had ended."""
        async with self._starting.setdefault(key, asyncio.Lock()):
            now = time.monotonic()
            for other, shell in list(self.shells.items()):
                if other != key and now - shell.used > self.idle_s:
                    del self.shells[other]
                    await shell.close()
            shell, note = self.shells.get(key), ""
            if shell is not None and not shell.alive:
                note = f"(the previous shell exited with code {shell.process.returncode}; this is a new one)\n"
                del self.shells[key]
                await shell.close()
                shell = None
            if shell is None:
                shell = Shell(self.argv, self.cwd)
                try:
                    await shell.start()
                except BaseException:
                    if shell.process is not None:
                        await shell.close()
                    raise
                self.shells[key] = shell
            shell.used = now
            return shell, note

    async def exchange(self, key: str, text: str, progress: Callable[[str], Awaitable[None]],
                       more: Callable[[], Iterable[str]] | None = None, markdown: bool = True,
                       max_chars: int = 2000) -> str:
        """Send `text` (empty: just look; `^C`: interrupt), show the output as it comes (every
        `update_s`), and return what appeared once it has been quiet for `quiet_s` or `max_wait_s` passed.
        `more` yields further input typed meanwhile, which is sent on as it arrives."""
        shell, note = await self.shell(key)
        mark = shell.shown
        tail = max(200, min(self.tail_chars, max_chars - 300))
        if text.strip():
            note += await self._send(shell, text)
            started = last_input = updated = time.monotonic()
            shown_total = shell.total
            try:
                while shell.alive:
                    await asyncio.sleep(self.poll_s)
                    for line in (more() if more is not None else ()):
                        note += await self._send(shell, line)
                        last_input = time.monotonic()
                    now = time.monotonic()
                    if (now - max(last_input, shell.last_output) >= self.quiet_s
                            or now - started >= self.max_wait_s):
                        break
                    if now - updated >= self.update_s and shell.total != shown_total:
                        updated, shown_total = now, shell.total
                        await progress(self.render(shell.since(mark), tail, markdown, "running…\n"))
            except asyncio.CancelledError:
                shell.interrupt()  # cancelling the run stops what it started
                raise
            await asyncio.sleep(0.1)  # let the last output arrive
        output = shell.since(mark)
        shell.shown = shell.total
        footer = ""
        if not shell.alive:
            footer = f"\n(shell exited with code {shell.process.returncode})"
        elif text.strip() != INTERRUPT and (running := shell.running()):
            footer = f"\n(still running: {', '.join(running[:3])}; `$t` shows more, `$t ^C` stops it)"
        empty = "(no new output)" if not text.strip() else "(no output)"
        return note + self.render(output, tail, markdown, empty=empty) + footer

    @staticmethod
    async def _send(shell: Shell, text: str) -> str:
        if text.strip() == INTERRUPT:
            return shell.interrupt()
        await shell.write(text)
        return ""

    @staticmethod
    def render(output: str, tail: int, markdown: bool, prefix: str = "", empty: str = "(no output)") -> str:
        output = clean(output[-tail * 4:]).strip("\n")  # only the end is shown: never clean more than that
        if not output.strip():
            return prefix + empty
        if len(output) > tail:
            output = "…" + output[-tail:]
        if not markdown:
            return prefix + output
        return prefix + "```\n" + output.replace("```", "ˋˋˋ") + "\n```"

    def kill_all(self) -> None:
        for shell in self.shells.values():
            shell.kill()

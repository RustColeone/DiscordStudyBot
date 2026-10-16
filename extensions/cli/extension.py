"""Built-in CLI interface: one local conversation over stdin/stdout."""

from __future__ import annotations

import asyncio
import sys
import time

from contracts import (Address, Contributions, Delivery, Display, Extension, Interface, Reply, Request, Sender)

NOTICES = {
    "run_failed": "[this request could not be finished]",
    "waiting_approval": "[waiting for the owner's approval {approval_id}]",
    "unknown_command": "[unknown command ${command}]",
    "not_permitted": "[not permitted here]",
    "relayed": "[relayed to {address}: ok={ok}]",
}


class CLIInterface(Interface):
    name = "cli"
    display = Display(markdown=True, edits=False, progress=True, quotes=False, files=True,
                      max_upload_mb=1024, max_text_chars=100_000)

    def __init__(self, conversation: str = "local", user: str = "local", user_name: str = "you",
                 stdin=None, stdout=None):
        self.conversation, self.user, self.user_name = conversation, user, user_name
        self._stdin, self._stdout = stdin or sys.stdin, stdout or sys.stdout
        self._task: asyncio.Task | None = None
        self._port = None
        self._counter = 0

    async def start(self, port) -> None:
        self._port = port
        self._task = asyncio.create_task(self._read_loop(), name="cli-input")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _read_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, self._stdin.readline)
            if not line:
                self._port.log("stdin_closed")
                return
            text = line.rstrip("\n")
            if not text.strip():
                continue
            self._counter += 1
            await self._port.submit(Request(
                id=f"{time.time_ns()}-{self._counter}", address=Address(self.name, self.conversation),
                sender=Sender(self.user, self.user_name), text=text, received_at=time.time(),
                display=self.display))

    async def deliver(self, reply: Reply) -> Delivery:
        origin = reply.data.get("origin_address")
        prefix = f"[{origin}] " if origin else ""
        if reply.kind == "text":
            line = f"{prefix}bot> {reply.text}"
        elif reply.kind == "file":
            line = f"{prefix}bot> [file {reply.file.name or reply.file.path}: {reply.file.path}] {reply.text}".rstrip()
        elif reply.kind == "progress":
            line = f"{prefix}  ... {reply.text}"
        elif reply.code == "reminder":
            line = f"{prefix}⏰ {reply.text}"
        elif reply.code in NOTICES:
            line = prefix + NOTICES[reply.code].format(**{"approval_id": "", "command": "", "address": "", "ok": "",
                                                          **reply.data})
        else:
            line = f"{prefix}[{reply.code or 'notice'}] {reply.text}".rstrip()
        self._stdout.write(line + "\n")
        self._stdout.flush()
        self._counter += 1
        return Delivery(True, message_id=f"out-{time.time_ns()}-{self._counter}")


def setup(context) -> Contributions:
    config = context.config
    return Contributions(interfaces=[CLIInterface(str(config.get("conversation", "local")),
                                                  str(config.get("user", "local")),
                                                  str(config.get("user_name", "you")))])


EXTENSION = Extension("cli", setup, contracts="1", version="1", description="Local stdin/stdout conversation")

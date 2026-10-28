"""Running git and shell commands without blocking the event loop."""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import system


class GitError(RuntimeError):
    def __init__(self, args, code: int, output: str):
        super().__init__(f"git {' '.join(args)} failed ({code}): {output.strip()[-600:]}")
        self.code, self.output = code, output


@dataclass(frozen=True)
class Completed:
    code: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool = False

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip("\n")


# Commands never wait for someone to type: no pager, no editor, no credential prompt.
QUIET_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_EDITOR": "true", "EDITOR": "true",
             "LC_ALL": os.environ.get("LC_ALL") or system.LOCALE}


SECRET_NAMES = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE|CREDENTIALS?)$", re.I)


def without_secrets(environment) -> dict:
    """The environment less every variable whose name says it holds a secret (the core's rule): a model's shell line
    (`env`, a test printing its environment) must not see the model keys."""
    return {key: value for key, value in environment.items() if not SECRET_NAMES.search(key)}


async def execute(argv: list[str], cwd: Path, *, timeout_s: float = 60.0, env: dict | None = None,
                  input_text: str | None = None, shell: bool = False, clean: bool = False,
                  isolation: system.Isolation | None = None) -> Completed:
    """Run one process (or a `bash -c` line with `shell`) in `cwd`; on timeout its whole process group ends.
    `clean` leaves secret variables out of its environment; `isolation` limits what it reaches (system.isolated)."""
    started = time.monotonic()
    full_env = {**(without_secrets(os.environ) if clean else os.environ), **QUIET_ENV, **(env or {})}
    command = system.shell(argv[0]) if shell else argv
    if isolation is not None:
        command = system.isolated(command, Path(cwd), isolation)
    process = await asyncio.create_subprocess_exec(
        *command, cwd=str(cwd), env=full_env, **system.group_options(),
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    timed_out = False
    try:
        out, err = await asyncio.wait_for(
            process.communicate(input_text.encode() if input_text is not None else None), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        _kill(process)
        out, err = await process.communicate()
    except asyncio.CancelledError:
        _kill(process)
        raise
    return Completed(process.returncode if not timed_out else -9, out.decode("utf-8", "replace"),
                     err.decode("utf-8", "replace"), round(time.monotonic() - started, 3), timed_out)


def _kill(process) -> None:
    system.end_group(process.pid)


async def git(cwd: Path, *args: str, check: bool = True, env: dict | None = None, input_text: str | None = None,
              timeout_s: float = 60.0) -> Completed:
    result = await execute(["git", *args], cwd, timeout_s=timeout_s, env=env, input_text=input_text)
    if check and result.code != 0:
        raise GitError(args, result.code, result.output)
    return result


async def out(cwd: Path, *args: str, **kwargs) -> str:
    return (await git(cwd, *args, **kwargs)).stdout.strip()

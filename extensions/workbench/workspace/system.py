"""Everything the lab asks of the operating system, in one place: a port (or Windows) reimplements only this.

- `shell(line)`: the argv that runs one shell line. bash everywhere (on Windows, Git for Windows' bash), so the
  allowlist, the models' commands and the tests mean the same thing on every system.
- `group_options()` / `end_group(process)`: start a process in its own group, and end the whole group (a test run
  that starts servers or subprocesses must not outlive its timeout); `detached_options()`: a background process
  that outlives the terminal (`club` starting a lab).
- `borrow(source, target)`: make a checkout's folder (a venv, node_modules) appear inside a worktree: a symlink, on
  Windows a directory junction when symlinks are not allowed, else a copy.
- `process_running(pid, needle)`: whether a process is alive and its command line contains `needle`;
  `stop_process(pid)`.
- `hand_over(argv, env)`: replace this process with another program (POSIX `execve`); on Windows start it, wait,
  and exit with its code.
- `offline(argv)`: the argv that runs `argv` with no network (Linux: a user and network namespace of its own, no
  root needed), for the commands models run when the lab has `command_network: false`; elsewhere it refuses.
- `isolated(argv, folder, isolation)`: the argv that runs `argv` as an `Isolation` allows: with `confined`
  (`command_filesystem: worktree`), in a sandbox that sees the system read-only, its own folder and the paths it
  shares, and no home or temp folders, no other processes (Linux: bubblewrap); elsewhere it refuses.
"""

from __future__ import annotations

import functools
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOWS = os.name == "nt"
# The locale commands run in, so git's messages read the same everywhere (macOS has no C.UTF-8).
LOCALE = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"


@dataclass(frozen=True)
class Isolation:
    """What a model's command may reach. `network`: the network. `confined`: only the system (read-only), its own
    folder, `readable` (read-only) and `shared` (writable); the home and temp folders, the lab's data, other
    worktrees and other processes are not there."""
    network: bool = True
    confined: bool = False
    readable: tuple[Path, ...] = ()
    shared: tuple[Path, ...] = ()


HIDDEN = ("/home", "/tmp", "/var/tmp", "/run/user", "/root", "/media", "/mnt")  # empty in a confined command


def isolated(argv: list[str], folder: Path, isolation: Isolation) -> list[str]:
    """`argv` run in `folder` as `isolation` allows (unchanged when it allows everything)."""
    if isolation.confined:
        if not shutil.which("bwrap") or not sys.platform.startswith("linux"):
            raise OSError("this system cannot confine a command to its worktree (it needs Linux and bubblewrap)")
        sandbox = ["bwrap", "--die-with-parent", "--new-session", "--unshare-all",
                   *(["--share-net"] if isolation.network else []), "--ro-bind", "/", "/", "--dev", "/dev",
                   "--proc", "/proc"]
        for hidden in HIDDEN:
            if os.path.isdir(hidden):
                sandbox += ["--tmpfs", hidden]
        for path in isolation.readable:  # parents before the folders inside them, so nothing is shadowed
            sandbox += ["--ro-bind", str(path), str(path)]
        for path in (*isolation.shared, folder):
            sandbox += ["--bind", str(path), str(path)]
        return [*sandbox, "--setenv", "HOME", "/tmp", "--chdir", str(folder), "--", *argv]
    return argv if isolation.network else offline(argv)


def offline(argv: list[str]) -> list[str]:
    """`argv` run with no network: what a model's command, probe or test may reach is this machine only."""
    prefix = _offline_prefix()
    if prefix is None:
        raise OSError("this system cannot run a command without network (it needs Linux user namespaces: unshare)")
    return [*prefix, *argv]


@functools.lru_cache(maxsize=1)
def _offline_prefix() -> tuple[str, ...] | None:
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return None
    for prefix in (("unshare", "--map-current-user", "--net", "--"), ("unshare", "--map-root-user", "--net", "--")):
        try:  # the same user inside where util-linux can (2.38+), else root mapped to her
            if subprocess.run([*prefix, "true"], capture_output=True, timeout=10).returncode == 0:
                return prefix
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def bash() -> str:
    """The bash to run shell lines with."""
    found = shutil.which("bash")
    if found and not (WINDOWS and found.lower().endswith(r"system32\bash.exe")):  # (WSL's bash runs elsewhere)
        return found
    if WINDOWS:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            candidate = Path(root) / "Git" / "bin" / "bash.exe"
            if root and candidate.is_file():
                return str(candidate)
    raise FileNotFoundError("bash was not found (on Windows, install Git for Windows)")


def shell(line: str) -> list[str]:
    return [bash(), "-c", line]


def group_options() -> dict:
    """Keyword arguments for subprocess / asyncio process creation: its own process group."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_options() -> dict:
    """Keyword arguments for subprocess.Popen: a background process that outlives this terminal and its Ctrl-C."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def end_group(pid: int) -> None:
    """End a process started with `group_options()` together with everything it started."""
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def borrow(source: Path, target: Path) -> None:
    """Make `source` (a folder or file of the checkout) appear at `target` inside a worktree."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.symlink_to(source, target_is_directory=source.is_dir())
        return
    except OSError:
        if not WINDOWS:
            raise
    if source.is_dir():  # Windows without symlink rights: a junction needs none
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(target), str(source)], capture_output=True)
        if made.returncode == 0:
            return
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def process_running(pid: int, needle: str = "") -> bool:
    """Whether `pid` is alive and (with `needle`) its command line mentions it."""
    try:
        import psutil
    except ImportError:  # without psutil: POSIX's /proc where there is one
        cmdline = Path(f"/proc/{pid}/cmdline")
        if not cmdline.exists():
            return False
        return not needle or needle in cmdline.read_bytes().decode(errors="replace").replace("\0", " ")
    try:
        process = psutil.Process(pid)
        return process.is_running() and (not needle or needle in " ".join(process.cmdline()))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def stop_process(pid: int) -> None:
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/T", "/PID", str(pid)], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def hand_over(argv: list[str], env: dict[str, str]) -> None:
    """Become `argv` (never returns)."""
    if WINDOWS:  # no real exec: run it and leave with its code
        sys.exit(subprocess.call(argv, env=env))
    os.execve(argv[0], argv, env)

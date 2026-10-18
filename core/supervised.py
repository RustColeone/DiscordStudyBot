"""The core's side of the supervisor (core/supervisor.py): heartbeats, the parent's health, and saying why
it restarted. Only active when the supervisor started this process (`CORE_HEARTBEAT` is set)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path

from contracts import Address, Reply

BEAT_S = 5.0


def supervised() -> bool:
    return bool(os.environ.get("CORE_HEARTBEAT"))


async def heartbeat(stop) -> None:
    """Touch the heartbeat file from the event loop (so a blocked loop stops beating), and stop the core if
    the supervisor is gone: an unsupervised bot must not keep running unwatched."""
    path = Path(os.environ["CORE_HEARTBEAT"])
    parent = int(os.environ.get("CORE_SUPERVISOR_PID") or 0)
    count = 0
    while True:
        count += 1
        try:
            path.write_text(str(count), encoding="utf-8")  # a counter: the supervisor watches it change
        except OSError:
            pass
        if parent and not parent_alive(parent):
            print("[core] The supervisor is gone; stopping.", flush=True)
            stop()
            return
        await asyncio.sleep(BEAT_S)


def parent_alive(pid: int) -> bool:
    if os.name != "nt" and os.getppid() != pid:
        return False  # reparented: the supervisor died
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        return True


def stop_on_signal(core, task):
    """The handler for SIGINT/SIGTERM. The first one stops cleanly. A second SIGTERM is ignored (systemd
    signals every process of the service and the supervisor passes signals on, so duplicates must not cut
    the clean shutdown short); a second Ctrl-C (SIGINT) is a person insisting, and ends it at once."""
    def handle(signum=None) -> None:
        if handle.done:
            if signum == signal.SIGINT:
                task.cancel()
            return
        handle.done = True
        if core.started:
            core.request_stop(0)  # run_forever returns and stops everything in order
        else:
            task.cancel()  # still starting up: nothing to shut down cleanly yet
    handle.done = False
    return handle


def request(by: str, where: str | None = None) -> None:
    """Record who asked for a restart, and in which conversation, for the next core to report."""
    path = os.environ.get("CORE_RESTART_REASON")
    if path:
        data = {"reason": "requested", "by": by, "at": time.time(), **({"where": where} if where else {})}
        Path(path).write_text(json.dumps(data), encoding="utf-8")


def describe(data: dict, now: float | None = None) -> str:
    down = max(0.0, (now or time.time()) - float(data.get("at") or now or time.time()))
    took = f"down {down:.0f} s" if down < 120 else f"down {down / 60:.0f} min"
    if data.get("reason") == "requested":
        who = f" {data['by']}" if data.get("by") else ""
        return f"🔄 Restarted as{who} asked ({took})."
    when = time.strftime("%H:%M", time.localtime(float(data.get("at") or time.time())))
    again = f" Restart {data['restarts']} in the last 10 minutes." if int(data.get("restarts") or 0) > 1 else ""
    if data.get("reason") == "hang":
        return f"⚠️ Restarted after it stopped responding at {when} ({took}).{again}"
    detail = " | ".join(str(line).strip() for line in data.get("detail") or [] if str(line).strip())[-400:]
    return (f"⚠️ Restarted after a crash at {when} (exit code {data.get('code')}, {took})."
            + (f"\n{detail}" if detail else "") + again)


async def report(core, wait_s: float = 180.0) -> None:
    """Tell the owner why the previous core stopped, once the control route's interface is connected; a
    requested restart is also answered in the conversation it was asked from (another bot's DM, say)."""
    path = Path(os.environ.get("CORE_RESTART_REASON") or "")
    if not path.name or not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    finally:
        path.unlink(missing_ok=True)
    text = describe(data)
    print(f"[core] {text}", flush=True)
    try:
        asked = Address.parse(data["where"]) if data.get("where") else None
    except ValueError:
        asked = None
    sent: set[Address] = set()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        control = core.manager.control if core.started else None
        targets = [] if control is None else [control]
        if asked is not None and core.started and asked not in targets:
            targets.append(asked)
        for target in targets:
            if target in sent:
                continue
            # Interfaces connect after the core starts (Discord logs in); try until the message goes out.
            reply = Reply(target, "notice", code="restarted", text=text, source="core",
                          data={"reason": str(data.get("reason", ""))})
            if (await core.gateway.deliver(reply)).ok:
                sent.add(target)
        if control is not None and set(targets) <= sent:
            return
        await asyncio.sleep(2)

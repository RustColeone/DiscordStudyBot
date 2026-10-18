"""`python -m core [--config core.yml]` runs the bot under its supervisor (core/supervisor.py), which restarts
it after a crash, a hang or `$admin restart`. `python -m core setup` sets it up; `python -m core serve` runs
the core alone (what the supervisor starts); `--check` validates everything and exits."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from core import supervised, supervisor
from core.app import Core, config_path
from core.config import CoreConfig


def describe(core: Core) -> None:
    configured = core.providers.configured()
    print(f"[core] modules: {', '.join(item.extension.name for item in core.extensions)}; "
          f"interfaces: {', '.join(core.gateway.interfaces) or 'none'}; "
          f"model providers with keys: {', '.join(configured) or 'none'}", flush=True)
    if not configured:
        print("[core] No model key yet, so questions cannot be answered (commands still work). Run "
              "`venv/bin/python -m core setup` to add one.", flush=True)


async def main(arguments: argparse.Namespace) -> int:
    if arguments.command == "setup":
        from core.wizard import Wizard
        # A --config that does not exist yet is created by the setup.
        return await Wizard(arguments.config or config_path(None)).run()
    path = config_path(arguments.config)
    core = Core(CoreConfig.load(path))
    print(f"[core] configuration: {path or 'built-in defaults (no core.yml)'}", flush=True)
    if arguments.check:
        await core.build()
        for loaded in core.extensions:
            contributions = loaded.contributions
            kind = "built-in" if core.config.builtin(loaded.path) else "external"
            print(f"{loaded.extension.name} ({kind}, {loaded.path}): "
                  f"interfaces={[i.name for i in contributions.interfaces]} "
                  f"engines={[e.spec.name for e in contributions.engines]} "
                  f"capabilities={[c.name for c in contributions.capabilities]}")
        print("core capabilities:", sorted(name for name in core.manager.capabilities if name.startswith("core.")))
        describe(core)
        core.tank.close()
        core.bottle.close()
        return 0
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    on_signal = supervised.stop_on_signal(core, task)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, on_signal, sig)
        except NotImplementedError:
            # Windows: asyncio.run already turns Ctrl-C into cancelling this task.
            break
    background = []
    if supervised.supervised():
        background.append(asyncio.create_task(supervised.heartbeat(lambda: core.request_stop(0))))
    try:
        await core.build()
        describe(core)
        if supervised.supervised():
            core.manager.restart = lambda by: (supervised.request(by), core.request_stop(supervisor.RESTART))
            background.append(asyncio.create_task(supervised.report(core)))
        await core.run_forever(idle_exit=arguments.idle_exit)
    except asyncio.CancelledError:
        return 0
    finally:
        for item in background:
            item.cancel()
    return core.exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the bot core with its configured modules.")
    parser.add_argument("command", nargs="?", choices=("run", "setup", "serve"), default="run",
                        help="run the bot under its supervisor (default); setup: keys, modules and settings, step "
                             "by step; serve: the core alone, without the supervisor")
    parser.add_argument("--config", default=None,
                        help="configuration merged over the built-in defaults (default: ./core.yml if present)")
    parser.add_argument("--check", action="store_true", help="load and validate extensions, then exit")
    parser.add_argument("--idle-exit", type=float, default=None,
                        help="exit after this many seconds with no activity (for scripted runs)")
    arguments = parser.parse_args()
    if arguments.command == "run" and not arguments.check:
        # The supervisor starts `serve` with the same options and restarts it when needed.
        sys.exit(supervisor.main([item for item in sys.argv[1:] if item != "run"]))
    sys.exit(asyncio.run(main(arguments)))

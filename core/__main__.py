"""Run the core: `python -m core --config core.yml`."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from core.app import Core, config_path
from core.config import CoreConfig


def describe(core: Core) -> None:
    configured = core.providers.configured()
    print(f"[core] extensions: {', '.join(item.extension.name for item in core.extensions)}; "
          f"interfaces: {', '.join(core.gateway.interfaces) or 'none'}; "
          f"model providers with keys: {', '.join(configured) or 'none'}", flush=True)
    if not configured:
        print("[core] No model provider key found. Set one of DEEPSEEK_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY "
              "or CLAUDE_API_KEY in .env; until then requests fail.", flush=True)


async def main(arguments: argparse.Namespace) -> int:
    path = config_path(arguments.config)
    core = Core(CoreConfig.load(path))
    print(f"[core] configuration: {path or 'built-in defaults (no core.yml)'}", flush=True)
    if arguments.check:
        await core.build()
        for loaded in core.extensions:
            contributions = loaded.contributions
            print(f"{loaded.extension.name} ({loaded.path}): "
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await core.build()
        describe(core)
        await core.run_forever(idle_exit=arguments.idle_exit)
    except asyncio.CancelledError:
        pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the bot core with its configured extensions.")
    parser.add_argument("--config", default=None,
                        help="configuration merged over the built-in defaults (default: ./core.yml if present)")
    parser.add_argument("--check", action="store_true", help="load and validate extensions, then exit")
    parser.add_argument("--idle-exit", type=float, default=None,
                        help="exit after this many seconds with no activity (for scripted runs)")
    sys.exit(asyncio.run(main(parser.parse_args())))

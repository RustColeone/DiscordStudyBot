"""Assembles the core: storage, providers, manager, gateway and extensions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.audit import Audit
from core.config import ConfigError, CoreConfig
from core.gateway.gateway import Gateway
from core.loader import LoadedExtension, load_extension
from core.manager.manager import Manager
from core.providers.registry import Providers
from core.storage.bottle import Bottle
from core.storage.tank import Tank


class Core:
    def __init__(self, config: CoreConfig, *, provider_adapters=None, tick_seconds: float = 1.0, clock=None):
        self.config = config
        self._provider_adapters = provider_adapters
        self._tick_seconds, self._clock = tick_seconds, clock
        self.extensions: list[LoadedExtension] = []
        self.started = False

    async def build(self) -> None:
        config = self.config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = Audit(config.data_dir / "audit.log", echo=config.audit_echo)
        self.tank = Tank(config.data_dir / "tank.db")
        self.bottle = Bottle(config.data_dir / "gateway.db")
        self.providers = Providers(config.providers, config.profiles, adapters=self._provider_adapters,
                                   record=self.audit.record)
        self.manager = Manager(self.tank, self.providers, personas=config.personas, assignments=config.assignments,
                               owners=config.owners, control=config.control, data_dir=config.data_dir,
                               audit=self.audit)
        options = {"tick_seconds": self._tick_seconds}
        if self._clock is not None:
            options["clock"] = self._clock
        self.gateway = Gateway(self.bottle, self.manager, audit=self.audit, rules=config.rules,
                               routes=config.routes, routines=config.routines, **options)
        names = set()
        for path in config.extensions:
            loaded = await load_extension(path, lambda name: config.extension_config.get(name, {}),
                                          config.data_dir / "extensions", self.audit.record)
            name = loaded.extension.name
            if name in names:
                raise ConfigError(f"Two extensions are named {name!r}")
            names.add(name)
            for interface in loaded.contributions.interfaces:
                self.gateway.register_interface(interface, name)
            for engine in loaded.contributions.engines:
                self.manager.register_engine(engine, name)
            for capability in loaded.contributions.capabilities:
                self.manager.register_capability(capability, name)
            for persona in loaded.contributions.personas:
                self.manager.register_persona(persona, name)
            self.extensions.append(loaded)
            self.audit.record("extension_loaded", extension=name, path=str(path),
                              interfaces=[i.name for i in loaded.contributions.interfaces],
                              engines=[e.spec.name for e in loaded.contributions.engines],
                              capabilities=[c.name for c in loaded.contributions.capabilities])
        self.manager.attach_gateway(self.gateway)
        problems = self.manager.problems()
        if config.control is not None and config.control.interface not in self.gateway.interfaces:
            problems.append(f"control route {config.control} uses an interface that is not loaded")
        if problems:
            raise ConfigError("; ".join(problems))

    async def start(self) -> None:
        if not hasattr(self, "manager"):
            await self.build()
        await self.gateway.start()
        await self.manager.start()
        self.started = True
        self.audit.record("core_started", extensions=[item.extension.name for item in self.extensions])

    async def stop(self) -> None:
        if not self.started:
            return
        self.started = False
        await self.manager.stop()
        await self.gateway.stop()
        self.audit.record("core_stopped")
        self.tank.close()
        self.bottle.close()
        self.audit.close()

    async def run_forever(self, idle_exit: float | None = None) -> None:
        await self.start()
        try:
            if idle_exit is None:
                await asyncio.Event().wait()
            quiet_since = asyncio.get_running_loop().time()
            seen = len(self.audit.events)
            while True:
                await asyncio.sleep(0.5)
                now = asyncio.get_running_loop().time()
                busy = self.manager._tasks or len(self.audit.events) != seen
                seen = len(self.audit.events)
                if busy:
                    quiet_since = now
                elif now - quiet_since >= idle_exit:
                    return
        finally:
            await self.stop()


def config_path(argument: str | None) -> Path | None:
    """An explicit --config must exist; otherwise core.yml in the working directory is optional."""
    if argument:
        path = Path(argument).resolve()
        if not path.is_file():
            raise ConfigError(f"{path} does not exist")
        return path
    default = Path("core.yml").resolve()
    return default if default.is_file() else None

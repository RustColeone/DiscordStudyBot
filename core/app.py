"""Assembles the core: storage, providers, manager, gateway and extensions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.audit import Audit
from core.config import ConfigError, CoreConfig
from core.gateway.gateway import Gateway
from core.loader import LoadedExtension, load_extension
from core.manager.manager import Manager
from core.manager.policy import Permissions, check_extension_permissions
from core.manager.secrets import Secrets
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
        self.exit_code = 0  # 75 asks the supervisor for a restart (core/supervisor.py)
        self._stopping = asyncio.Event()

    async def build(self) -> None:
        try:
            await self._build()
        except Exception:
            for store in (getattr(self, "tank", None), getattr(self, "bottle", None)):
                if store is not None:
                    store.close()
            raise

    async def _build(self) -> None:
        config = self.config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit = Audit(config.data_dir / "audit.log", echo=config.audit_echo)
        self.tank = Tank(config.data_dir / "tank.db")
        self.bottle = Bottle(config.data_dir / "gateway.db")
        self.providers = Providers(config.providers, {}, adapters=self._provider_adapters,
                                   record=self.audit.record, configured=config.configured_providers)
        self.manager = Manager(self.tank, self.providers, personas=config.personas, assignments=config.assignments,
                               owners=config.owners, control=config.control, data_dir=config.data_dir,
                               audit=self.audit, persona_parts=config.persona_parts)
        options = {"tick_seconds": self._tick_seconds}
        if self._clock is not None:
            options["clock"] = self._clock
        self.gateway = Gateway(self.bottle, self.manager, audit=self.audit, rules=config.rules,
                               routes=config.routes, routines=config.routines, **options)
        names = set()
        for path in config.modules():
            loaded = await load_extension(path, lambda name: config.extension_config.get(name, {}),
                                          config.data_dir / "extensions", self.audit.record)
            name = loaded.extension.name
            if name in names:
                raise ConfigError(f"Two modules are named {name!r}; exclude one (`modules: {{exclude: [...]}}`)")
            names.add(name)
            for interface in loaded.contributions.interfaces:
                self.gateway.register_interface(interface, name)
            for engine in loaded.contributions.engines:
                self.manager.register_engine(engine, name)
            for capability in loaded.contributions.capabilities:
                self.manager.register_capability(capability, name)
            for persona in loaded.contributions.personas:
                self.manager.register_persona(persona, name)
            for part in loaded.contributions.prompt_parts:
                self.manager.register_prompt_part(part, name)
            self._register_defaults(loaded)
            self.extensions.append(loaded)
            self.audit.record("extension_loaded", extension=name, path=str(path), builtin=config.builtin(path),
                              interfaces=[i.name for i in loaded.contributions.interfaces],
                              engines=[e.spec.name for e in loaded.contributions.engines],
                              capabilities=[c.name for c in loaded.contributions.capabilities])
        # Configured profiles may use providers contributed by any loaded module.
        for profile, candidates in config.profiles.items():
            self.providers.add_profile(profile, candidates)
        self.manager.attach_gateway(self.gateway)
        self.manager.permissions = self._permissions()
        self.manager.secrets = self._secrets()
        from core.setup import Setup
        self.manager.setup_desk = Setup(config)
        problems = self.manager.problems()
        if config.control is not None and config.control.interface not in self.gateway.interfaces:
            problems.append(f"control route {config.control} uses an interface that is not loaded")
        if problems:
            raise ConfigError("; ".join(problems))
        for warning in self.manager.prompt_part_warnings():
            self.audit.record("prompt_part_problem", problem=warning)

    def _permissions(self) -> Permissions:
        """Built-in defaults, extension defaults (code, then the module's config.yml), then core.yml, setting by
        setting."""
        layers = [("built-in", self.config.builtin_permissions)]
        for loaded in self.extensions:
            name = loaded.extension.name
            own = {interface.name for interface in loaded.contributions.interfaces}
            for source, layer in ((f"{name} default", loaded.contributions.permissions),
                                  (f"{name} config.yml", (loaded.config or {}).get("permissions"))):
                if not layer:
                    continue
                try:
                    check_extension_permissions(name, own, layer)
                except ValueError as error:
                    raise ConfigError(f"{source}: {error}") from error
                layers.append((source, layer))
        layers.append(("core.yml", self.config.permissions))
        legacy = self.manager.legacy_use()
        if legacy:
            layers.append(("runtime assignment (allow/deny)", legacy))
        try:
            return Permissions(layers)
        except ValueError as error:
            raise ConfigError(str(error)) from error

    def _secrets(self) -> Secrets:
        names = {variable for loaded in self.extensions for setting in loaded.extension.settings
                 if setting.kind == "secret" for variable in setting.env}
        names.update(self.providers.key_variables())  # configured and contributed providers alike
        paths = [str(path / "config.yml") for path in self.config.all_modules()]
        paths += [str(self.config.data_dir / pattern) for pattern in ("*.db", "*.db-wal", "*.db-shm",
                                                                       "extensions/*/*.db*", "extensions/*/keys.json",
                                                                       "extensions/*/session.key")]
        # A relative `secrets:` entry is under the home folder (as it always was) and, since prompt part files are
        # relative to core.yml's folder, under that folder too.
        relative = [str(self.config.path(item)) for item in self.config.secret_paths
                    if not item.startswith(("*", "/", "~"))]
        return Secrets.collect(env_files=self.config.env_files, names=names,
                               paths=[*paths, *self.config.secret_paths, *relative])

    def _register_defaults(self, loaded: LoadedExtension) -> None:
        """Provider kinds an extension adds; providers, profiles, assignments and admission it suggests (core.yml
        and runtime edits win)."""
        contributions, name = loaded.contributions, loaded.extension.name
        own = {interface.name for interface in contributions.interfaces}
        for pattern in [*contributions.assignments, *contributions.admission]:
            if pattern.split(":", 1)[0] not in own or ":" not in pattern:
                raise ConfigError(f"{name}: defaults may only cover its own interfaces, not {pattern!r}")
        # Kinds, then providers, then profiles: each may use the ones before.
        for kind, factory in contributions.adapters.items():
            try:
                self.providers.add_kind(kind, factory)
            except ValueError as error:
                raise ConfigError(f"{name}: {error}") from error
        for provider, settings in contributions.providers.items():
            self.providers.add_provider(provider, settings)
        for profile, candidates in contributions.profiles.items():
            if profile not in self.config.profiles:
                self.providers.add_profile(profile, candidates)
        for pattern, value in contributions.assignments.items():
            self.manager.default_assignment(pattern, value)
        stored = self.bottle.rules()
        for pattern, rule in contributions.admission.items():
            if pattern not in self.config.rules and pattern not in stored:
                self.gateway.admin().set_rule(pattern, dict(rule))

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

    def request_stop(self, exit_code: int = 0) -> None:
        """End run_forever cleanly; exit_code 75 asks the supervisor to start a fresh core."""
        self.exit_code = exit_code
        self._stopping.set()

    async def run_forever(self, idle_exit: float | None = None) -> None:
        await self.start()
        try:
            if idle_exit is None:
                await self._stopping.wait()
                return
            quiet_since = asyncio.get_running_loop().time()
            seen = len(self.audit.events)
            while not self._stopping.is_set():
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

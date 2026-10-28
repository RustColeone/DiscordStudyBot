"""Assembles the core: storage, providers, manager, gateway and extensions."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from types import ModuleType

from contracts import Extension
from core.audit import Audit
from core.config import ConfigError, CoreConfig
from core.dependencies import load_order, missing_package
from core.gateway.gateway import Gateway
from core.loader import ExtensionError, LoadedExtension, import_extension, load_extension
from core.manager.manager import Manager
from core.manager.policy import Permissions, check_extension_permissions
from core.manager.renames import adopt_engine_names, adopt_former_names
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
        self.skipped: dict[str, str] = {}  # module → why it is not loaded (contracts 1.10)
        self._formerly: dict[str, str] = {}  # a module's former name → the module (contracts 1.11)
        self.set_aside: list[tuple[str, str, str]] = []  # (module or "core", pattern, why): defaults not applied
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
        # Who registered each engine and persona, to check that a module declares the modules whose names it uses.
        self._owners: dict[tuple[str, str], str] = {("persona", name): "core" for name in config.personas}
        found: dict[str, tuple[Path, Extension, ModuleType]] = {}
        for path in config.modules():
            try:
                extension, module = import_extension(path)
            except ExtensionError as error:
                self._skip(path.name, str(error), path)
                continue
            if extension.name in found:
                raise ConfigError(f"Two modules are named {extension.name!r}; exclude one (`modules: {{exclude: [...]}}`)")
            found[extension.name] = (path, extension, module)
        # A module's former names (Extension.formerly): a declaration naming one is given that module.
        self._formerly = {old: name for name, item in found.items() for old in item[1].formerly if old not in found}
        order, cyclic = load_order({name: item[1] for name, item in found.items()}, self._formerly)
        for name, why in cyclic.items():
            self._skip(name, why, found[name][0])
        for name in order:
            await self._load(*found[name])
        # Configured profiles may use providers contributed by any loaded module.
        for profile, candidates in config.profiles.items():
            self.providers.add_profile(profile, candidates)
        self.manager.attach_gateway(self.gateway)
        self.manager.permissions = self._permissions()
        self.manager.secrets = self._secrets()
        from core.setup import Setup
        self.manager.setup_desk = Setup(config)
        adopt_former_names(self.manager)  # (Persona.formerly) before the assignments are checked
        adopt_engine_names(self.manager)  # (EngineSpec.formerly) likewise
        self._set_aside_builtin_defaults()
        problems = self.manager.problems()
        if config.control is not None and config.control.interface not in self.gateway.interfaces:
            problems.append(f"control route {config.control} uses an interface that is not loaded")
        if problems:
            raise ConfigError("; ".join(problems))
        for warning in self.manager.prompt_part_warnings():
            self.audit.record("prompt_part_problem", problem=warning)

    def _skip(self, name: str, why: str, path: Path) -> None:
        """A module that cannot load is left out with the reason; the rest of the core goes on (`--check` fails)."""
        self.skipped[name] = why
        self.audit.record("module_skipped", extension=name, path=str(path), reason=why)

    def _module(self, name: str) -> str:
        """The module a declared name stands for: itself, or the module that had it before (Extension.formerly)."""
        return self._formerly.get(name, name)

    async def _load(self, path: Path, extension: Extension, module: ModuleType) -> None:
        name, config = extension.name, self.config
        loaded_names = {item.extension.name for item in self.extensions}
        why = self._unmet(extension, loaded_names)
        if why:
            return self._skip(name, why, path)
        present = frozenset(item for item in extension.uses if self._module(item) in loaded_names)
        self._adopt_module_names(extension)
        try:
            loaded = await load_extension(path, lambda module_name: config.extension_config.get(module_name, {}),
                                          config.data_dir / "extensions", self.audit.record,
                                          self.manager.facts.handle, present, (extension, module),
                                          lambda: self.manager.persona_views(config.profiles))
        except ExtensionError as error:
            return self._skip(name, str(error), path)
        why, aside = self._check_names(loaded, loaded_names)
        if why:
            return self._skip(name, why, path)
        if aside:
            loaded.contributions = dataclasses.replace(loaded.contributions, assignments={
                pattern: value for pattern, value in loaded.contributions.assignments.items() if pattern not in aside})
            for pattern, reason in aside.items():
                self.set_aside.append((name, pattern, reason))
                self.audit.record("default_unusable", extension=name, pattern=pattern, reason=reason)
        saved = self._registries()
        try:
            self._check_defaults(loaded)
            self._register(loaded)
        except (ValueError, ConfigError) as error:
            self._restore(saved)
            return self._skip(name, str(error), path)
        self._register_defaults(loaded)
        self.extensions.append(loaded)
        self.audit.record("extension_loaded", extension=name, path=str(path), builtin=config.builtin(path),
                          interfaces=[i.name for i in loaded.contributions.interfaces],
                          engines=[e.spec.name for e in loaded.contributions.engines],
                          capabilities=[c.name for c in loaded.contributions.capabilities],
                          watchers=[w.name for w in loaded.contributions.watchers],
                          requires=list(extension.requires), uses=sorted(present))

    def _unmet(self, extension: Extension, loaded_names: set[str]) -> str | None:
        """Why the module cannot load (something it requires is missing), or None."""
        for item in extension.requires:
            if item.startswith("package:"):
                missing = missing_package(item)
                if missing:
                    return f"it requires {missing}"
            elif self._module(item) not in loaded_names:
                reason = self.skipped.get(self._module(item))
                return (f"it requires the {item} module, which was not loaded ({reason})" if reason
                        else f"it requires the {item} module, which is not installed or is excluded")
        return None

    def _adopt_module_names(self, extension: Extension) -> None:
        """Extension.formerly: the stores and facts of a module it had the name of are its own (its data folder and
        settings are the loader's to move and read)."""
        for old in extension.formerly:
            moved = {"stores": self.tank.rename_module(old, extension.name),
                     "facts": self.manager.facts.rename_module(old, extension.name)}
            if any(moved.values()):
                self.audit.record("module_renamed", extension=extension.name, old=old, **moved)

    def _check_names(self, loaded: LoadedExtension, loaded_names: set[str]) -> tuple[str | None, dict[str, str]]:
        """Every engine and persona the module's suggested assignments name must be its own, the core's, or a
        declared module's. Returns why the module is refused (an undeclared name), and the suggested assignments to
        set aside (they name what a module it uses would add, and that module is not here)."""
        extension, contributions = loaded.extension, loaded.contributions
        declared = {self._module(item) for item in (*extension.requires, *extension.uses)}
        absent = [item for item in extension.uses if self._module(item) not in loaded_names]
        own = {("engine", engine.spec.name) for engine in contributions.engines}
        own |= {("persona", persona.name) for persona in contributions.personas}
        aside: dict[str, str] = {}
        for pattern, value in contributions.assignments.items():
            value = value if isinstance(value, dict) else {}
            named = [("engine", value.get("engine"))] + [("persona", item) for item in value.get("personas") or []]
            for kind, item in named:
                if not item or (kind, item) in own:
                    continue
                owner = self._owners.get((kind, item))
                if owner is None and absent:
                    aside.setdefault(pattern, f"the {kind} {item!r} is not loaded (without {', '.join(absent)})")
                elif owner is None:
                    return (f"its default for {pattern!r} names the {kind} {item!r}, which no loaded module adds and "
                            "no module it declares could add (add that module to `requires` or `uses`)"), {}
                elif owner != "core" and owner not in declared:
                    return (f"its default for {pattern!r} names the {kind} {item!r} of the {owner} module, which it "
                            f"does not declare (add {owner!r} to `requires` or `uses`)"), {}
        return None, aside

    def _check_defaults(self, loaded: LoadedExtension) -> None:
        """What _register_defaults would refuse, checked before anything is registered."""
        contributions, name = loaded.contributions, loaded.extension.name
        own = {interface.name for interface in contributions.interfaces}
        for pattern in [*contributions.assignments, *contributions.admission]:
            if pattern.split(":", 1)[0] not in own or ":" not in pattern:
                raise ConfigError(f"defaults may only cover its own interfaces, not {pattern!r}")
        for kind, factory in contributions.adapters.items():
            if kind in self.providers._kinds or not callable(factory):
                raise ConfigError(f"provider kind {kind!r} " + ("already exists" if kind in self.providers._kinds
                                                                 else "needs an adapter factory"))

    def _register(self, loaded: LoadedExtension) -> None:
        name, contributions = loaded.extension.name, loaded.contributions
        for interface in contributions.interfaces:
            self.gateway.register_interface(interface, name)
        for engine in contributions.engines:
            self.manager.register_engine(engine, name)
            self._owners[("engine", engine.spec.name)] = name
            for old in engine.spec.formerly:
                self._owners.setdefault(("engine", old), name)
        for capability in contributions.capabilities:
            self.manager.register_capability(capability, name)
        for persona in contributions.personas:
            self.manager.register_persona(persona, name)
            self._owners.setdefault(("persona", persona.name), name)  # the first module's persona wins
        for part in contributions.prompt_parts:
            self.manager.register_prompt_part(part, name)
        for watcher in contributions.watchers:
            self.manager.register_watcher(watcher, name)

    def _registries(self) -> list:
        """The registries a module adds to, copied, so a module that fails halfway can be taken out again."""
        gateway, manager = self.gateway, self.manager
        return [(store, type(store)(store)) for store in (
            gateway.interfaces, gateway._origin, manager.engines, manager.engine_formerly, manager._origin,
            manager.capabilities,
            manager.personas, manager.prompt_parts, manager._part_origin, manager.watchers, self._owners)]

    @staticmethod
    def _restore(saved: list) -> None:
        for store, copy in saved:
            if isinstance(store, dict):
                store.clear()
                store.update(copy)
            else:
                store[:] = copy

    def _set_aside_builtin_defaults(self) -> None:
        """The core's own default assignments (`*`, `routine:*`) apply only while what they name is loaded."""
        for pattern in self.config.builtin_assignments:
            assignment = self.manager.assignments.get(pattern)
            if assignment is None or pattern in self.manager._overrides.keys():
                continue
            missing = ([f"the engine {assignment.engine!r}"] if self.manager.engine(assignment.engine) is None else []) \
                + [f"the persona {item!r}" for item in assignment.personas if item not in self.manager.personas]
            if missing:
                self.manager._config_assignments.pop(pattern, None)
                reason = f"{' and '.join(missing)} not loaded"
                self.set_aside.append(("core", pattern, reason))
                self.audit.record("default_unusable", extension="core", pattern=pattern, reason=reason)

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
        and runtime edits win). _check_defaults has checked them."""
        contributions = loaded.contributions
        # Kinds, then providers, then profiles: each may use the ones before.
        for kind, factory in contributions.adapters.items():
            self.providers.add_kind(kind, factory)
        for provider, settings in contributions.providers.items():
            self.providers.add_provider(provider, settings)
        for profile, candidates in contributions.profiles.items():
            if profile not in self.config.profiles:
                try:
                    self.providers.add_profile(profile, candidates)
                except ValueError as error:  # a provider nobody has (yet): that profile is left out
                    self.audit.record("profile_unusable", extension=loaded.extension.name, profile=profile,
                                      reason=str(error))
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

"""Guided setup: `python -m core setup`. Asks, checks and saves everything a fresh install needs.

1. Model keys (each tested with a one-word request before it is saved to .env).
2. Which modules load (switching one writes `modules.exclude` in core.yml).
3. What the loaded modules ask for on first run (the Discord token, logged in to check it).
4. Any other module setting, on request.
5. A final load of everything, as `--check` does.

Secrets are typed hidden and go only to .env (mode 600); other settings go to the module's config.yml.
Everything saved stays saved if the setup is stopped halfway.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Awaitable, Callable

from contracts import ModelRequest, Setting
from core.config import ConfigError, CoreConfig
from core.setup import Module, Setup, parse, set_env, show


def yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


class Wizard:
    def __init__(self, path: str | Path | None = None, *, ask: Callable[[str], str] = input,
                 secret: Callable[[str], str] = getpass.getpass, say: Callable[[str], None] = print,
                 test_key: Callable[[str, str], Awaitable[tuple[bool, str]]] | None = None):
        self.ask, self.secret, self.say = ask, secret, say
        self.config = CoreConfig.load(path)
        self.setup = Setup(self.config)
        if path is not None:
            self.setup.file = Path(path).resolve()
        self.test_key = test_key or self._test_key

    def where(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(Path.cwd()))
        except ValueError:
            return str(path)

    async def run(self) -> int:
        self.say("Setting up the bot. Press Enter to keep what is there; what you save stays saved even if you "
                 "stop halfway (Ctrl-C).")
        try:
            await self.models()
            self.modules()
            await self.first_run()
            await self.more()
        except (KeyboardInterrupt, EOFError):
            self.say("\nStopped. What was saved stays.")
            return 1
        return await self.verify()

    # ---- 1. model keys --------------------------------------------------------------------------
    def providers(self) -> dict[str, str]:
        return {name: str(settings["api_key_env"]) for name, settings in self.config.providers.items()
                if settings.get("api_key_env")}

    async def models(self) -> None:
        providers = self.providers()
        self.say("\n1. Model keys: the bot needs at least one to answer questions.")
        for name, env in providers.items():
            self.say(f"   {name:<11} {env:<22} {'set' if os.environ.get(env, '').strip() else 'not set'}")
        while True:
            name = self.ask("   Add or replace a key: provider name, or Enter to go on: ").strip().lower()
            if not name:
                break
            if name not in providers:
                self.say(f"   Unknown provider. Choose from: {', '.join(providers)}.")
                continue
            key = self.secret(f"   {name} key (typing is hidden): ").strip()
            if not key:
                continue
            ok, note = await self.test_key(name, key)
            self.say(f"   {'✓' if ok else '✗'} {note}")
            if not ok and not yes(self.ask("   Save it anyway? [y/N] ")):
                continue
            set_env(self.config.env_file, providers[name], key)
            self.say(f"   Saved to {self.where(self.config.env_file)}.")
        if not any(os.environ.get(env, "").strip() for env in providers.values()):
            self.say("   No model key yet: the bot will run commands but cannot answer questions.")

    async def _test_key(self, provider: str, key: str) -> tuple[bool, str]:
        from core.providers.registry import Providers
        model = next((candidate["model"] for candidates in self.config.profiles.values() for candidate in candidates
                      if candidate.get("provider") == provider), None)
        if model is None:
            return True, "not tested (no model of this provider is configured)"
        env = self.providers()[provider]
        previous = os.environ.get(env)
        os.environ[env] = key
        try:
            registry = Providers(self.config.providers, {"check": [{"provider": provider, "model": model}]})
            # Reasoning models spend tokens before answering, so allow a normal budget.
            await registry.generate(ModelRequest([{"role": "user", "content": "Reply with the single word OK."}],
                                                 max_tokens=1500, timeout_s=45), "check")
            return True, f"{model} answered"
        except Exception as error:
            if "truncated" in str(error).lower():
                return True, f"{model} answered (cut short, but the key was accepted)"
            return False, f"{model} did not answer: {str(error)[:200]}"
        finally:
            if previous is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = previous

    # ---- 2. modules -----------------------------------------------------------------------------
    def modules(self) -> None:
        self.say("\n2. Modules: everything loads unless switched off.")
        for module in self.setup.modules():
            self.say(f"   {'off' if module.excluded else 'on ':<3}  {module.name:<11} "
                     f"{'built-in' if module.builtin else 'external':<8}  {module.description}")
        answer = self.ask("   Modules to switch on or off (names, separated by spaces), or Enter to keep: ")
        for name in answer.replace(",", " ").split():
            module = self.setup.module(name)
            if module is None:
                self.say(f"   No module {name!r}.")
                continue
            try:
                path = self.setup.enable(module, module.excluded)
            except (ValueError, OSError) as error:
                self.say(f"   ✗ Not saved: {error}")
                continue
            self.say(f"   {module.name} is now {'on' if module.excluded else 'off'} ({self.where(path)}).")

    # ---- 3 and 4. module settings ---------------------------------------------------------------
    async def first_run(self) -> None:
        self.say("\n3. What the modules need.")
        asked = False
        for module in self.setup.modules():
            if module.excluded:
                continue
            for setting in module.settings:
                if setting.ask:
                    asked = True
                    await self.edit(module, setting)
        if not asked:
            self.say("   Nothing is required.")

    async def more(self) -> None:
        while True:
            name = self.ask("\n4. Change a module's other settings? Its name, or Enter to finish: ").strip()
            if not name:
                return
            module = self.setup.module(name)
            if module is None or not module.settings:
                self.say("   No such module." if module is None else
                         "   It is off; switch it on (step 2, or run this again) to set it up." if module.excluded
                         else "   It has no settings.")
                continue
            for setting in module.settings:
                value, source = self.setup.value(module, setting)
                shown = ("set" if value else "not set") if setting.kind == "secret" else show(value)
                self.say(f"   {setting.key:<18} {shown:<24} {setting.description}")
            key = self.ask("   Setting to change, or Enter: ").strip()
            setting = module.setting(key) if key else None
            if key and setting is None:
                self.say(f"   {module.name} has no setting {key!r}.")
            elif setting is not None:
                await self.edit(module, setting)

    async def edit(self, module: Module, setting: Setting) -> None:
        label = f"{module.name}.{setting.key}"
        while True:
            value, source = self.setup.value(module, setting)
            self.say(f"   {label}: {setting.description}")
            if setting.kind == "secret":
                raw = self.secret(f"   {label} ({'set' if value else 'not set'}; typing is hidden; Enter keeps): ")
            else:
                raw = self.ask(f"   {label} [{show(value)}] (Enter keeps; 'default' resets): ")
            raw = raw.strip()
            if not raw:
                return
            if raw.lower() == "default" and setting.kind != "secret":
                try:
                    self.say(f"   Back to the default ({self.where(self.setup.write(module, setting, None))}).")
                except (ValueError, OSError) as error:
                    self.say(f"   ✗ Not saved: {error}")
                return
            try:
                parsed = parse(setting, raw)
            except ValueError as error:
                self.say(f"   {error}.")
                continue
            if setting.check is not None:
                try:
                    self.say(f"   ✓ {await setting.check(str(parsed))}")
                except ValueError as error:
                    self.say(f"   ✗ {error}")
                    if not yes(self.ask("   Save it anyway? [y/N] ")):
                        continue
                except Exception as error:
                    self.say(f"   (could not check it: {type(error).__name__})")
            try:
                self.say(f"   Saved to {self.where(self.setup.write(module, setting, parsed))}.")
            except (ValueError, OSError) as error:
                self.say(f"   ✗ Not saved: {error}")
                return
            if source == "core.yml":
                self.say(f"   Note: extension_config.{module.name}.{setting.key} in core.yml overrides it.")
            return

    # ---- 5. check -------------------------------------------------------------------------------
    async def verify(self) -> int:
        from core.app import Core
        self.say("\n5. Checking that everything loads…")
        try:
            config = CoreConfig.load(self.setup.file if self.setup.file.exists() else None)
            core = Core(config)
            await core.build()
        except (ConfigError, Exception) as error:
            self.say(f"   ✗ {type(error).__name__}: {error}")
            self.say("   Fix it (run the setup again, or edit the file it names), then start the bot.")
            return 1
        try:
            self.say(f"   ✓ modules: {', '.join(item.extension.name for item in core.extensions)}")
            self.say(f"   ✓ interfaces: {', '.join(core.gateway.interfaces) or 'none'}")
            self.say(f"   ✓ model providers with keys: {', '.join(core.providers.configured()) or 'none'}")
        finally:
            core.tank.close()
            core.bottle.close()
        command = "venv/bin/python -m core"
        if self.setup.file.exists() and self.setup.file != Path("core.yml").resolve():
            command += f" --config {self.where(self.setup.file)}"
        self.say(f"\nDone. Start the bot with: {command}")
        self.say("At this console you are the bot's creator. On Discord, the application's owner is (or `owners` "
                 "in core.yml). Later changes: run this again, or `$setup` in a chat with the bot.")
        return 0

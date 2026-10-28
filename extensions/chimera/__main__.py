"""Run the Classroom on its own: the bot's real core, with only this module and the students (and any you add with
--also).

    venv/bin/python -m extensions.chimera --env ../.env --key DEEPSEEK_API_KEY   # only that key from the file
    venv/bin/python -m extensions.chimera --demo     # no keys: a scripted club plays the practice task
    venv/bin/python -m extensions.chimera --also webui --also momochat   # MomoChat next door

It writes a small core configuration (<data>/core/core.yml) and starts `python -m core` with it, under the core's
supervisor, so the Classroom runs exactly as it does inside the bot. Settings come from extensions/chimera/config.yml
(see config.example.yml); --config adds more.

    venv/bin/python -m extensions.chimera install --env ../.env --key DEEPSEEK_API_KEY

puts the `club` command on your PATH: run it in any project folder to work there from the terminal (cli.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def read_env(path: Path, only: set[str] | None = None) -> dict[str, str]:
    """KEY=VALUE lines of a .env file (with `only`, just those names). The values are never printed."""
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if not only or key in only:
            found[key] = value.strip().strip("\"'")
    return found


def also(names: list[str]) -> list[str]:
    """The modules asked for with --also, after the students: they are the Classroom's seats (and who talks in
    MomoChat next door), and their pictures are what the pages show. Without mneme-llm they are not loaded, and the
    seats are those of `team:`."""
    return list(dict.fromkeys(["students", *names]))


def data_folder(arguments) -> Path:
    """--data, else data/chimera (data/workbench when only that one exists: the Classroom's folder before)."""
    if arguments.data:
        return Path(arguments.data).expanduser().resolve()
    before = ROOT / "data" / "workbench"
    return before if before.is_dir() and not (ROOT / "data" / "chimera").exists() else ROOT / "data" / "chimera"


def load_config(paths: list[Path]) -> dict:
    import yaml
    config: dict = {}
    for path in paths:
        if path.is_file():
            config.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return config


def merge(base: dict, override: dict) -> dict:
    """Mappings merged key by key, anything else replaced (as core.yml merges over the defaults)."""
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


PATH_KEYS = ("data_dir", "env_files", "modules")  # settings of a --core file that name files: relative to it


def core_file(path: Path) -> dict:
    """A --core file, its relative paths (data_dir, env_files, modules) made relative to the file itself."""
    document = load_config([path])
    folder = path.resolve().parent
    here = lambda value: str((folder / Path(str(value)).expanduser()).resolve()) if value else value
    if document.get("data_dir"):
        document["data_dir"] = here(document["data_dir"])
    if isinstance(document.get("env_files"), list):
        document["env_files"] = [here(item) for item in document["env_files"]]
    modules = document.get("modules")
    if isinstance(modules, dict):
        for key in ("dirs", "paths"):
            if isinstance(modules.get(key), list):
                modules[key] = [here(item) for item in modules[key]]
    return document


def module_path(name: str) -> Path:
    for folder in (ROOT / "extensions", ROOT / "plugins"):
        if (folder / name).is_dir():
            return folder / name
    raise SystemExit(f"No module named {name!r} in extensions/ or plugins/.")


def core_config(arguments) -> tuple[dict, dict[str, str]]:
    """The Classroom-only core configuration, and the model keys to hand it (only those named with --key)."""
    data = data_folder(arguments)
    lab = load_config([HERE / "config.yml", *(Path(item).expanduser() for item in arguments.config)])
    lab["data_dir"] = str(data)  # the board, projects and worktrees stay where they were
    if arguments.port is not None:
        lab["port"] = arguments.port
    if arguments.host:
        lab["host"] = arguments.host
    if arguments.demo:
        lab["demo"] = True
    only = set(arguments.key) or None
    keys: dict[str, str] = {}
    for path in [*(Path(item).expanduser() for item in arguments.env), ROOT / ".env"]:  # the ones you name win
        if path.is_file():
            for key, value in read_env(path, only).items():
                keys.setdefault(key, value)
    for name in arguments.key:  # a key you name may come from your shell too
        if name not in keys and os.environ.get(name):
            keys[name] = os.environ[name]
    config = {"data_dir": str(data / "core"),        # the core's own stores: runs, approvals, history
              "env_files": [],                        # keys come only from what this launcher hands over
              # the others first, as in the bot (alphabetical there): the students are the ones who speak
              "modules": {"dirs": [], "paths": [*(str(module_path(name)) for name in also(arguments.also)),
                                                str(HERE)]},
              "extension_config": {"chimera": lab}}
    generated = list(config["modules"]["paths"])
    for path in arguments.core:  # more core settings (other modules' extension_config, owners, ...)
        config = merge(config, core_file(Path(path).expanduser()))
    modules = config.setdefault("modules", {})  # (a --core module list adds to the Classroom's own, never drops it)
    modules["paths"] = list(dict.fromkeys([*(modules.get("paths") or []), *generated]))
    return config, keys


def handed_environment(keys: dict[str, str], environ=None) -> dict[str, str]:
    """What the core starts with: this environment less every key, token or password in it, plus `keys` (the ones
    you named). A key exported in your shell never reaches the core unless you name it."""
    from .workspace.git import without_secrets
    return {**without_secrets(os.environ if environ is None else environ), **keys}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["install"]:  # put `club` on the PATH: the Classroom from any folder (see cli.py)
        from .cli import main as club
        return club(argv)
    parser = argparse.ArgumentParser(prog="python -m extensions.chimera", description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, help="port for the page (default 4157)")
    parser.add_argument("--host", help="address to listen on (default all; 127.0.0.1 for this computer only)")
    parser.add_argument("--data", help="the Classroom's data folder (default data/chimera)")
    parser.add_argument("--env", action="append", default=[], help="another .env file with model keys")
    parser.add_argument("--key", action="append", default=[], metavar="NAME",
                        help="hand over only this key, from the .env files or your shell (repeat for more); no other "
                             "key or token of your shell reaches the core")
    parser.add_argument("--config", action="append", default=[], help="another YAML file of the Classroom's settings")
    parser.add_argument("--demo", action="store_true", help="a scripted club: no keys, no model calls")
    parser.add_argument("--also", action="append", default=[], metavar="MODULE",
                        help="another module to load (e.g. webui and momochat: MomoChat next door)")
    parser.add_argument("--core", action="append", default=[], metavar="FILE",
                        help="more core settings (YAML), merged over the generated ones: other modules' settings")
    parser.add_argument("--serve", action="store_true", help="run the core without its supervisor")
    parser.add_argument("--print-config", action="store_true", help="print the core configuration and exit")
    arguments = parser.parse_args(argv)
    config, keys = core_config(arguments)
    if arguments.print_config:
        print(json.dumps(config, indent=2, default=str))
        print(f"keys handed over: {', '.join(sorted(keys)) or 'none'}", file=sys.stderr)
        return 0
    path = Path(config["data_dir"]) / "core.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    import yaml
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")  # (dates stay dates)
    if arguments.demo:
        print("[classroom] Demo mode: a scripted club, no model calls. Try the practice project's Sunday task.",
              flush=True)
    else:
        print(f"[classroom] Model keys handed to the core: {', '.join(sorted(keys)) or 'none (try --env or --demo)'}",
              flush=True)
    environment = handed_environment(keys)
    os.chdir(ROOT)
    command = [sys.executable, "-m", "core", *(["serve"] if arguments.serve else []), "--config", str(path)]
    from .workspace import system
    system.hand_over(command, environment)  # the core takes over this process
    return 0


if __name__ == "__main__":
    sys.exit(main())

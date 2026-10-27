"""Students extension: the characters themselves, one folder each (persona prompt, display name, models).

Every folder with a `student.yml` is a student. Engines (the room, the assistant, the workbench) and interfaces use
them by name; this module only describes who they are and which models they prefer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from contracts import Contributions, Extension, Persona, Setting

HERE = Path(__file__).resolve().parent


def compose(folder: Path, files) -> str:
    """A student's prompt: her `prompt:` files in order, each file's lines joined with nothing between them.

    The prompts are Chinese prose, so a line break is only for reading: blank lines are dropped, and a space that
    belongs in the prompt is written at the start of its line."""
    text, root = [], folder.resolve().parent
    for name in files:
        path = (folder / name).resolve()
        if root not in path.parents:
            raise ValueError(f"{folder.name}: prompt file {name!r} is outside the students folder")
        text.extend(line for line in path.read_text(encoding="utf-8").splitlines() if line)
    return "".join(text)


def read_students(root: Path = HERE) -> list[dict]:
    """Every student's `student.yml`, with her prompt composed, in `order`."""
    found = []
    for folder in sorted(path for path in root.iterdir() if (path / "student.yml").is_file()):
        data = yaml.safe_load((folder / "student.yml").read_text(encoding="utf-8")) or {}
        if data.get("name") != folder.name:
            raise ValueError(f"{folder.name}/student.yml: `name` must be the folder's name")
        if not data.get("models"):
            raise ValueError(f"{folder.name}/student.yml: no `models`")
        found.append({**data, "prompt": compose(folder, data.get("prompt") or ["prompt.md"])})
    return sorted(found, key=lambda data: (data.get("order", 1000), data["name"]))


def profiles(students: list[dict]) -> dict[str, list[dict]]:
    """Each student prefers her own models, then the others' first models in `fallback` order (candidates without a
    key are skipped, so one key is always enough). `falls_back_to: <name>` puts that student's whole list after her
    own instead."""
    ranked = sorted((data for data in students if data.get("fallback") is not None), key=lambda data: data["fallback"])
    shared = [data["models"][0] for data in ranked]
    result = {}
    for data in students:
        own = list(data["models"])
        result[data["name"]] = [*own, *(item for item in shared if item not in own)]
    for data in students:
        if data.get("falls_back_to"):
            own = list(data["models"])
            result[data["name"]] = [*own, *(item for item in result[data["falls_back_to"]] if item not in own)]
    return result


STUDENTS = read_students()
# A provider a student brings (Grok's xAI, say; the others are built into the core) asks for its key at setup.
KEYS = [(provider, settings["api_key_env"], data) for data in STUDENTS
        for provider, settings in (data.get("providers") or {}).items() if settings.get("api_key_env")]
DISPLAY = {data["name"]: data["display_name"] for data in STUDENTS}


def key_setting(provider: str, env: str, data: dict) -> Setting:
    fallback = f"{DISPLAY[data['falls_back_to']]}'s model" if data.get("falls_back_to") else "another model"
    return Setting(f"{provider}_api_key", f"API key for {data['display_name']} (`{provider}`; without it, {fallback} "
                   "answers for her)", "secret", env=env)


def setup(context) -> Contributions:
    config = context.config
    personas = [Persona(data["name"], data["display_name"], data["prompt"], profile=data["name"]) for data in STUDENTS]
    # The students' own providers and profiles; this module's config.yml `providers:` (key by key) and `profiles:`
    # override them, and core.yml overrides both.
    providers = {}
    for data in STUDENTS:
        for name, settings in (data.get("providers") or {}).items():
            providers[name] = dict(settings)
    for name, settings in (config.get("providers") or {}).items():
        providers[name] = {**providers.get(name, {}), **dict(settings)}
    return Contributions(personas=personas, providers=providers,
                         profiles={**profiles(STUDENTS), **dict(config.get("profiles") or {})})


EXTENSION = Extension("students", setup, contracts="1", version="1",
                      description="The students: one folder each with her prompt and models",
                      settings=tuple(key_setting(*item) for item in KEYS))

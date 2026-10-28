"""Students extension: the characters themselves, one folder each (persona prompt, display name, models, pictures),
and each one's memory of the people she talks with (memory.py, one mneme-llm file per student).

An agent is a persona with her models; a student is an agent with her memory, always. Every folder with a
`student.yml` is a student. Engines (MomoChat, Chimera) and interfaces use them by name; this module describes who they
are, what they look like and which models they prefer, and keeps what each remembers of you, whichever engine she
spoke in. It needs mneme-llm 0.2 or newer: without it, it is not loaded (and says why), and so there are no students.
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


def pictures(folder: Path, given) -> dict[str, str]:
    """Her `pictures:` (kind → file in her folder) as absolute paths; a missing file is left out until it exists."""
    found = {}
    for kind, name in dict(given or {}).items():
        path = (folder / str(name)).resolve()
        if folder.resolve() not in path.parents:
            raise ValueError(f"{folder.name}: picture {name!r} is outside her folder")
        if path.is_file():
            found[str(kind)] = str(path)
    return found


def read_students(root: Path = HERE) -> list[dict]:
    """Every student's `student.yml`, with her prompt composed and her pictures found, in `order`."""
    found = []
    for folder in sorted(path for path in root.iterdir() if (path / "student.yml").is_file()):
        data = yaml.safe_load((folder / "student.yml").read_text(encoding="utf-8")) or {}
        if data.get("name") != folder.name:
            raise ValueError(f"{folder.name}/student.yml: `name` must be the folder's name")
        if not data.get("models"):
            raise ValueError(f"{folder.name}/student.yml: no `models`")
        found.append({**data, "prompt": compose(folder, data.get("prompt") or ["prompt.md"]),
                      "pictures": pictures(folder, data.get("pictures"))})
    return sorted(found, key=lambda data: (data.get("order", 1000), data["name"]))


def profiles(students: list[dict]) -> dict[str, list[dict]]:
    """Each student prefers her own models, then the others' first models in `fallback` order (candidates without a
    key are skipped, so one key is always enough). `falls_back_to: <name>` puts that student's whole list after her
    own instead. Her tiers (Persona.profiles) are `<name>_own`, her own models only, and `<name>_strong`, her
    `strong:` models, when she has them."""
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
    for data in students:
        result[f"{data['name']}_own"] = list(data["models"])
        if data.get("strong"):
            result[f"{data['name']}_strong"] = list(data["strong"])
    return result


def tiers(data: dict) -> dict[str, str]:
    """Her model profiles by tier (contracts.TIERS)."""
    return {"own": f"{data['name']}_own", **({"strong": f"{data['name']}_strong"} if data.get("strong") else {})}


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
    from . import memory  # (mneme-llm, which `requires` has made sure of)
    config = context.config
    personas = [Persona(data["name"], data["display_name"], data["prompt"], profile=data["name"],
                        aliases=tuple(data.get("aliases") or ()), formerly=tuple(data.get("formerly") or ()),
                        pictures=data["pictures"], description=str(data.get("description") or ""),
                        profiles=tiers(data)) for data in STUDENTS]
    # The students' own providers and profiles; this module's config.yml `providers:` (key by key) and `profiles:`
    # override them, and core.yml overrides both.
    providers = {}
    for data in STUDENTS:
        for name, settings in (data.get("providers") or {}).items():
            providers[name] = dict(settings)
    for name, settings in (config.get("providers") or {}).items():
        providers[name] = {**providers.get(name, {}), **dict(settings)}
    remembered = memory.contributions(context, {persona.name: {"display_name": persona.display_name,
                                                               "aliases": list(persona.aliases)}
                                                for persona in personas})
    return Contributions(personas=personas, providers=providers,
                         profiles={**profiles(STUDENTS), **dict(config.get("profiles") or {})}, **remembered)


EXTENSION = Extension("students", setup, contracts="1", version="2", requires=("package:mneme-llm>=0.2",),
                      formerly=("companion",),
                      description="The students: one folder each with her prompt, models and pictures, and her memory "
                                  "of each person (mneme-llm)",
                      settings=tuple(key_setting(*item) for item in KEYS))

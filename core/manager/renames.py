"""Former names: a persona renamed keeps what was hers (Persona.formerly, contracts 1.9), and so does an engine
(EngineSpec.formerly, 1.11).

At start, once every module is loaded, each old name no persona has now is read as hers: core.yml's assignments
and prompt parts that name it, and everything the core keeps under it (her memory, the modules' stores scoped by
her, diary notes, `$model` choices, runtime assignments, facts, open runs, her name on what she said). An engine's
old name is read as the engine in core.yml's assignments and the runtime ones, and its open runs carry on in it. It
runs on every start and moves only what is still under the old name, so it is done once.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.manager.manager import Manager


def former_names(personas) -> dict[str, str]:
    """old name → the persona who had it, for names no persona has now."""
    found: dict[str, str] = {}
    for persona in personas.values():
        for old in persona.formerly:
            if old not in personas:
                found.setdefault(old, persona.name)
    return found


def adopt_former_names(manager: "Manager") -> dict[str, str]:
    renames = former_names(manager.personas)
    if not renames:
        return renames
    for pattern, assignment in list(manager._config_assignments.items()):
        old = [name for name in assignment.personas if name in renames]
        if old:
            manager._config_assignments[pattern] = dataclasses.replace(
                assignment, personas=tuple(renames.get(name, name) for name in assignment.personas))
            manager.audit.record("persona_renamed_in_config", pattern=pattern, old=old,
                                 new=[renames[name] for name in old],
                                 hint="core.yml names her by her old name; it is read as her new one (rename it there)")
    overrides = manager._overrides
    for pattern in overrides.keys():
        value = dict(overrides.get(pattern) or {})
        if any(name in renames for name in value.get("personas") or ()):
            overrides.set(pattern, {**value, "personas": [renames.get(name, name) for name in value["personas"]]})
    choices = manager.model_choices.store
    for address in choices.keys():
        value = dict(choices.get(address) or {})
        if any(name in renames for name in value):
            renamed = {renames[name]: choice for name, choice in value.items() if name in renames}
            kept = {name: choice for name, choice in value.items() if name not in renames}
            choices.set(address, {**renamed, **kept})  # a choice made under her new name wins
    for old, new in renames.items():
        moved = manager.tank.rename_persona(old, new, manager.personas[new].display_name)
        moved["facts"] = manager.facts.rename(old, new)
        if any(moved.values()):
            manager.audit.record("persona_data_moved", old=old, new=new, **moved)
    return renames


def adopt_engine_names(manager: "Manager") -> dict[str, str]:
    """Assignments naming an engine by a name it had before (EngineSpec.formerly) name it now; its open runs are its."""
    renames = {old: new for old, new in manager.engine_formerly.items() if old not in manager.engines}
    if not renames:
        return renames
    for pattern, assignment in list(manager._config_assignments.items()):
        if assignment.engine in renames:
            manager._config_assignments[pattern] = dataclasses.replace(assignment, engine=renames[assignment.engine])
            if pattern not in manager._module_patterns:
                manager.audit.record("engine_renamed_in_config", pattern=pattern, old=assignment.engine,
                                     new=renames[assignment.engine],
                                     hint="core.yml names the engine by its old name; it is read as its new one "
                                          "(rename it there)")
    overrides = manager._overrides
    for pattern in overrides.keys():
        value = dict(overrides.get(pattern) or {})
        if value.get("engine") in renames:
            overrides.set(pattern, {**value, "engine": renames[value["engine"]]})
    for old, new in renames.items():
        moved = manager.tank.rename_engine(old, new)
        if moved:
            manager.audit.record("engine_runs_moved", old=old, new=new, runs=moved)
    return renames

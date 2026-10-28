"""Chimera: the students work on your code together, the way HydraFusion does, with the classmates in view. It is
the heavy-work harness (MomoChat is the conversation one); it was the workbench until 2026-10-27.

Each task is routed to the simplest workflow likely to be enough (one model, a cheap draft with a quality gate, or
a draft reviewed by another model family and revised once), worked in its own git worktree, tested, and shown to you
as one change set. Nothing reaches your branch until you have checked it and approved it.

It contributes the `chimera` engine (formerly `workbench`), the `chimera.*` capabilities (creator only), the
Classroom page (the `classroom` interface, on port 4157), a model profile per classmate and tier, and defaults for its
own conversations. Its seats are the students (`uses=("students",)`: each on her own and strong models), changed or
added to by `team:`; seats of its own are agents it adds. `python -m extensions.chimera` runs the core with only this
module (and the students).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from contracts import Contributions, Extension, Setting

from .board import Board
from .capabilities import Chimera, build
from .demo import DemoAdapter
from .engine import ChimeraEngine
from .record import Records
from .team import build_team, family_of
from .web.server import ClassroomUI
from .workspace.projects import Registry
from .workspace.worktree import Worktrees

HERE = Path(__file__).resolve().parent
# Pictures of the classmates dropped in this module's own avatars/; the students' own pictures come from the core.
AVATARS = [HERE / "avatars"]
DEFAULTS: dict[str, Any] = {
    "interface": "classroom", "port": 4157, "title": "Club Room", "second_approval": True, "commands": "allowlist",
    "baseline": True, "sample": True, "web": True, "api": True,
    "budget": {"model_calls": 120, "tool_calls": 400, "seconds": 3600},
}


def settings(config: Mapping[str, Any]) -> dict:
    merged = {**DEFAULTS, **dict(config or {})}
    merged["budget"] = {**DEFAULTS["budget"], **dict((config or {}).get("budget") or {})}
    return merged


def assemble(context) -> tuple[Contributions, dict]:
    """Everything the module is made of (the tests use the parts directly)."""
    config = settings(context.config)
    # The page's passphrase: the web UI's by default, so one passphrase lets you into the club's pages (none: anyone
    # on the local network).
    config["passphrase"] = str(config.get("passphrase") or os.getenv(str(config.get("passphrase_env")
                                                                          or "WEBUI_PASSPHRASE"), "")).strip()
    # Where the Classroom keeps its board, projects and worktrees: the module's data folder unless `data_dir` names one
    # (the Classroom on its own keeps data/chimera). Absolute: git commands run from inside each project.
    data_dir = Path(config.get("data_dir") or context.data_dir).expanduser().resolve()
    demo = config.get("demo") is True
    # The board keeps its file name from when this was the workbench, so the labs and data folders made then open.
    board = Board(data_dir / "workbench.db")
    name = str(config["interface"])
    if name != "workbench":  # its tasks' conversations were `workbench:<task>` before the page was the classroom
        board.readdress("workbench", name)
    registry = Registry(config, board, data_dir)
    trees = Worktrees(data_dir / "worktrees")
    # The students (the agents of the students module, loaded before this one), and the seats `team:` adds.
    team = build_team(config, context.personas() if callable(getattr(context, "personas", None)) else ())
    used = sorted({candidate["provider"] for seat in team.seats.values() for candidate in (*seat.cheap, *seat.strong)})
    if demo:  # the scripted club: each provider's models answered by the demo, families kept for the reviews
        config["families"] = {**{f"demo_{name}": family_of(name) for name in used}, **dict(config.get("families") or {})}
    team.ready = {key: bool(value) for key, value in dict(board.get("ready") or {}).items()}  # as last seen
    # Each student's record (sessions landed and discarded, her level) and benchmark runs, as facts for the pages.
    records = Records(board, team, getattr(context, "facts", None))  # (no facts when loaded without a core)
    records.publish()
    board.subscribe(records.listen)
    chimera = Chimera(board, registry, trees, config, team)
    prices = getattr(context, "prices", None)  # the core's price list (contracts 1.12; core.yml `prices:`)
    interfaces = []
    page = None
    if config.get("api", True) is not False:  # the Classroom's interface: its API, and the page unless `web: false`
        page = ClassroomUI(name, config, board, registry, trees, team, data_dir, AVATARS, prices, chimera)
        interfaces.append(page)
    engine = ChimeraEngine(board, registry, team, config, link=page.link if page else None, prices=prices)
    if page is not None:
        chimera.link = page.link
    providers = {key: dict(value) for key, value in dict(config.get("providers") or {}).items()}
    profiles = team.profiles()
    if demo:
        providers = {f"demo_{name}": {"kind": "chimera_demo"} for name in sorted({*used, *providers})}
        profiles = {name: [{**candidate, "provider": f"demo_{candidate['provider']}"} for candidate in candidates]
                    for name, candidates in profiles.items()}
    assignment = {"engine": "chimera", "personas": list(team.seats), "budget": dict(config["budget"]),
                  "options": dict(config.get("options") or {})}
    if page is not None and not team.seats:
        context.log("no_seats", hint="no students and no seats under team:; the Classroom has nobody to work")
    contributions = Contributions(
        interfaces=interfaces, engines=[engine], capabilities=build(chimera),
        personas=team.personas(), profiles=profiles,  # the agents of the seats team: adds
        assignments={f"{name}:*": assignment} if page and team.seats else {},
        admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": False}} if page else {},
        providers=providers, adapters={"chimera_demo": lambda settings: DemoAdapter()} if demo else {})
    return contributions, {"board": board, "registry": registry, "trees": trees, "team": team, "engine": engine,
                           "page": page, "chimera": chimera, "settings": config, "providers": providers,
                           "prices": prices, "records": records}


def setup(context) -> Contributions:
    return assemble(context)[0]


EXTENSION = Extension("chimera", setup, contracts="1", version="2", uses=("students",), formerly=("workbench",),
                      description="The students work on your code together: routed, reviewed, tested, landed on your "
                                  "approval",
                      settings=(
    Setting("port", "Port the Classroom page is served on", "number", 4157),
    Setting("title", "The page's title", "text", "Club Room"),
    Setting("passphrase", "Passphrase asked once per browser (default: the web UI's; empty: anyone on the local "
            "network)", "secret", env="WEBUI_PASSPHRASE"),
    Setting("second_approval", "Ask for a checklist check before the final approval to land a change", "boolean",
            True),
    Setting("commands", "Shell lines the club may run without asking: allowlist, ask (always ask) or any", "text",
            "allowlist"),
    Setting("minor_needs_failing_input", "A reviewer's minor correctness, requirement or scope note sends the change "
            "back only when it names an input that fails (else it is a note on your checklist)", "boolean", False),
    Setting("command_network", "Commands the club runs (run, probes, the tests) may reach the network; false runs "
            "them with none (Linux; elsewhere they are refused)", "boolean", True),
    Setting("command_filesystem", "What the commands the club runs may see: all, or worktree (the system read-only, "
            "their own worktree and the project's checkout; no home or temp folders, no other processes; Linux with "
            "bubblewrap, elsewhere they are refused)", "text", "all"),
    Setting("default_project", "Project for tasks that name none (Discord, say)", "text"),
    Setting("data_dir", "Where the Classroom keeps its board, projects and worktrees (default: the module's data "
            "folder)", "text"),
    Setting("demo", "A scripted club that plays the practice task: no keys, no model calls", "boolean", False),
    Setting("artificial_analysis_api_key", "Artificial Analysis API key (free, artificialanalysis.ai) for model prices",
            "secret", env="ARTIFICIAL_ANALYSIS_API_KEY"),
))

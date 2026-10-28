"""Workbench: the club works on your code, the way HydraFusion does, with the classmates in view.

Each task is routed to the simplest workflow likely to be enough (one model, a cheap draft with a quality gate, or
a draft reviewed by another model family and revised once), worked in its own git worktree, tested, and shown to you
as one change set. Nothing reaches your branch until you have checked it and approved it.

It contributes the `workbench` engine, the `workbench.*` capabilities (creator only), the workbench page (an
interface on port 4157), default classmates (the room's win where it is loaded), a model profile per classmate and
tier, and defaults for its own conversations. `python -m extensions.workbench` runs the core with only this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from contracts import Contributions, Extension, Setting

from .board import Board
from .capabilities import Workbench, build
from .demo import DemoAdapter
from .engine import WorkbenchEngine
from .prices import PriceBook
from .team import build_team, family_of
from .web.server import WorkbenchUI
from .workspace.projects import Registry
from .workspace.worktree import Worktrees

HERE = Path(__file__).resolve().parent
# Pictures of the classmates dropped in this module's own avatars/; the students' own pictures come from the core.
AVATARS = [HERE / "avatars"]
# The providers the default team uses (as the core and the room define them; configured settings win).
PROVIDERS = {
    "deepseek": {"kind": "openai_compatible", "base_url": "https://api.deepseek.com", "api_key_env": "DEEPSEEK_API_KEY",
                 "schema_mode": "json_object", "effort_param": True, "inline_history": True},
    "openai": {"kind": "openai_compatible", "api_key_env": "OPENAI_API_KEY", "schema_mode": "native",
               "token_param": "max_completion_tokens", "effort_param": True},
    "gemini": {"kind": "gemini", "api_key_env": "GEMINI_API_KEY", "min_interval_s": 1.1},
    "claude": {"kind": "anthropic", "api_key_env": "CLAUDE_API_KEY", "adaptive_thinking": True},
    "grok": {"kind": "openai_compatible", "base_url": "https://api.x.ai/v1", "api_key_env": "GROK_API_KEY",
             "schema_mode": "native", "effort_param": True},
}
DEFAULTS: dict[str, Any] = {
    "interface": "workbench", "port": 4157, "title": "Club Room", "second_approval": True, "commands": "allowlist",
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
    # Where the Classroom keeps its board, projects and worktrees: the module's data folder unless `data_dir` names one
    # (the Classroom on its own keeps data/workbench). Absolute: git commands run from inside each project.
    data_dir = Path(config.get("data_dir") or context.data_dir).expanduser().resolve()
    demo = config.get("demo") is True
    if demo:  # the scripted club: each provider's models answered by the demo, families kept for the reviews
        config["families"] = {**{f"demo_{name}": family_of(name) for name in PROVIDERS}, **dict(config.get("families") or {})}
    board = Board(data_dir / "workbench.db")
    registry = Registry(config, board, data_dir)
    trees = Worktrees(data_dir / "worktrees")
    team = build_team(config)
    team.ready = {key: bool(value) for key, value in dict(board.get("ready") or {}).items()}  # as last seen
    workbench = Workbench(board, registry, trees, config, team)
    prices = PriceBook(data_dir / "prices.json", config)
    name = str(config["interface"])
    interfaces = []
    page = None
    if config.get("api", True) is not False:  # the Classroom's interface: its API, and the page unless `web: false`
        page = WorkbenchUI(name, config, board, registry, trees, team, data_dir, AVATARS, prices, workbench)
        interfaces.append(page)
    engine = WorkbenchEngine(board, registry, team, config, link=page.link if page else None, prices=prices)
    if page is not None:
        workbench.link = page.link
    providers = {key: dict(value) for key, value in PROVIDERS.items()}
    for key, value in dict(config.get("providers") or {}).items():
        providers[key] = {**providers.get(key, {}), **dict(value)}
    profiles = team.profiles()
    if demo:
        providers = {f"demo_{name}": {"kind": "workbench_demo"} for name in providers}
        profiles = {name: [{**candidate, "provider": f"demo_{candidate['provider']}"} for candidate in candidates]
                    for name, candidates in profiles.items()}
    assignment = {"engine": "workbench", "personas": list(team.seats), "budget": dict(config["budget"]),
                  "options": dict(config.get("options") or {})}
    contributions = Contributions(
        interfaces=interfaces, engines=[engine], capabilities=build(workbench),
        # Registered only where no module before this one named them: with the room loaded, its classmates speak.
        personas=team.personas(), profiles=profiles,
        assignments={f"{name}:*": assignment} if page else {},
        admission={f"{name}:*": {"enabled": True, "require_mention_in_groups": False}} if page else {},
        providers=providers, adapters={"workbench_demo": lambda settings: DemoAdapter()} if demo else {})
    return contributions, {"board": board, "registry": registry, "trees": trees, "team": team, "engine": engine,
                           "page": page, "workbench": workbench, "settings": config, "providers": providers,
                           "prices": prices}


def setup(context) -> Contributions:
    return assemble(context)[0]


EXTENSION = Extension("workbench", setup, contracts="1", version="1", uses=("students",),
                      description="The club works on your code: routed, reviewed, tested, landed on your approval",
                      settings=(
    Setting("port", "Port the workbench page is served on", "number", 4157),
    Setting("title", "The page's title", "text", "Club Room"),
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

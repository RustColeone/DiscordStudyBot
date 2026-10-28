"""Describe the lab module's surface as language-neutral JSON (docs/spec/chimera.json): its capabilities, settings,
workflows, the JSON schemas its models answer in, every prompt and narration line, the routing table and limits,
the command allowlist, and the board's tables. Together with SPEC.md and PROTOCOL.md it is what a port implements;
tests/test_spec.py fails when it and the code drift apart.

    venv/bin/python tools/describe_module.py            # write docs/spec/chimera.json
    venv/bin/python tools/describe_module.py --check    # exit 1 if it is out of date
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "spec" / "chimera.json"
sys.path.insert(0, str(ROOT))


def describe() -> dict:
    from extensions.chimera import board, extension, team, workflows
    from extensions.chimera.kernel import flow, prompts, router
    from extensions.chimera.workspace import commands
    with tempfile.TemporaryDirectory() as folder:
        context = SimpleNamespace(name="chimera", config={}, data_dir=Path(folder), log=lambda *a, **k: None)
        contributions, parts = extension.assemble(context)
        capabilities = [{
            "name": item.name, "description": item.description, "parameters": dict(item.parameters),
            "required": list(item.required), "risk": item.risk.value, "access": item.access,
            "internal": item.internal, "never_auto": item.never_auto, "confirm": item.confirm,
            "command": item.command.name if item.command else None,
            "command_argument": item.command_argument} for item in contributions.capabilities]
        parts["board"].close()
    settings = [{"key": item.key, "description": item.description, "kind": item.kind, "default": item.default,
                 **({"env": item.env} if getattr(item, "env", None) else {})} for item in extension.EXTENSION.settings]
    return {
        "module": extension.EXTENSION.name,
        "defaults": extension.DEFAULTS,
        "settings": settings,
        "capabilities": capabilities,
        # The seats are the students (ExtensionContext.personas), changed or added to by `team:`.
        "team": {"seats": "students", "roles": team.DEFAULT_ROLES, "families": team.FAMILIES},
        "workflows": workflows.catalog(),
        "routing": router.ROUTING,
        "turn_limits": flow.TURNS,
        "sibling_roles": flow.SIBLING,
        "schemas": {"turn": prompts.turn_schema(prompts.WRITE_TOOLS),
                    "probe_turn": prompts.turn_schema(prompts.PROBE_TOOLS, findings=True),
                    "review": prompts.REVIEW_SCHEMA, "route": prompts.ROUTE_SCHEMA,
                    "requirements": prompts.REQUIREMENTS_SCHEMA},
        "tools": {name: {"description": text, "parameters": parameters}
                  for name, (text, parameters) in prompts.TOOLS.items()},
        "tool_sets": {"write": list(prompts.WRITE_TOOLS), "read": list(prompts.READ_TOOLS),
                      "probe": list(prompts.PROBE_TOOLS)},
        "review_categories": {"all": prompts.CATEGORIES, "blocking": list(prompts.BLOCKING)},
        "prompts": {name: getattr(prompts, name) for name in
                    ("SOLVER", "ASKER", "TESTER", "CRITIC", "JUDGE", "ROUTER", "MEMBER", "REQUIREMENTS")},
        "roles": prompts.ROLES,
        "lines": {key: {"en": english, "zh": chinese} for key, (english, chinese) in prompts.LINES.items()},
        "allowlist": {"programs": sorted(commands.PROGRAMS),
                      "subcommands": {key: sorted(value) for key, value in commands.SUBCOMMANDS.items()}},
        "board_schema": board.SCHEMA.strip(),
        "task_states": {"open": list(board.OPEN), "closed": list(board.CLOSED), "at_rest": list(board.AT_REST)},
    }


def render() -> str:
    return json.dumps(describe(), indent=1, ensure_ascii=False, default=str) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        sys.exit(0 if OUT.is_file() and OUT.read_text(encoding="utf-8") == text else 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")

"""The workbench's capabilities: the hands a task works with, and the checkpoints that need the creator.

| Capability | Tier | Who | Does |
| --- | --- | --- | --- |
| `workbench.open` | prepare | engine | make the task's worktree (its own branch, from the checkout's HEAD) |
| `workbench.list_files`, `read_file`, `search`, `diff` | read | engine | look (a question reads the checkout itself) |
| `workbench.write_file`, `edit_file`, `delete_file` | prepare | engine | change files in the worktree only |
| `workbench.run`, `workbench.test` | prepare; critical off the allowlist | engine | a shell line / the project's tests, in the worktree |
| `workbench.review` (`$review`) | write, never automatic | creator | the second check: "I looked at all of it" |
| `workbench.land` (`$land`) | critical, never automatic, confirmed | creator | the final approval: the change set becomes a commit |
| `workbench.discard` (`$discard`) | write | creator | throw a task's change set away |
| `workbench.tasks` (`$tasks`) | read | creator, any conversation | the Logbook: what the club did in the Classroom |
| `workbench.projects` (`$projects`) | read | creator, any conversation | the projects the club may work in |
| `workbench.invite` | prepare | creator, any conversation | an invitation to the Classroom: brief, notes, the chat's latest part; a link |

Review, land and discard are `internal`: your `$commands`, never offered to a model.

Every one is for the creator only, as everything that touches the computer is. The engine adds the task to each
call; a tool refuses a task that belongs to another conversation.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from contracts import Capability, Command, FileRef, Option, Risk, ToolResult

from . import views
from .board import CLOSED
from .kernel.flow import new_id
from .workspace import commands as allowlist
from .workspace import system, tools
from .workspace.git import GitError
from .workspace.projects import Registry, detect_test, with_test
from .workspace.worktree import PROBE, LandError, Worktrees

CHECKABLE = ("review", "approved")  # a change set waiting for the second check or the final approval
TASK = {"task": {"type": "string", "description": "the task ID (added by the engine)"}}
TEST_PATTERNS = [
    re.compile(r"^(Ran \d+ tests? in [\d.]+s)\s*$", re.M),                           # unittest, then OK/FAILED
    re.compile(r"^=+ (.*?(?:passed|failed|error|no tests ran).*?) =+\s*$", re.M | re.I),  # pytest
    re.compile(r"^(Tests?:\s+.+)$", re.M),                                           # jest
    re.compile(r"^(test result: .+)$", re.M),                                        # cargo
]


# What differs between two runs of the same line without meaning anything: where they ran, how long they took,
# memory addresses. A probe compares outputs with these masked.
NOISE = [(re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b"), "<time>"), (re.compile(r"\b0x[0-9a-fA-F]{6,}\b"), "0x…"),
         (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?"), "<now>")]


def comparable(output: str, *roots: Path) -> str:
    for root in roots:
        output = output.replace(str(root), "<root>").replace(Path(root).as_posix(), "<root>")
    for pattern, replacement in NOISE:
        output = pattern.sub(replacement, output)
    return output


def test_summary(output: str, code: int) -> str:
    """One line for a test run: the runner's own count where it can be found."""
    for pattern in TEST_PATTERNS:
        found = pattern.findall(output)
        if found:
            text = found[-1].strip()
            if text.startswith("Ran "):
                after = output[output.rfind(text) + len(text):].strip().splitlines()
                text += f" · {after[0].strip()}" if after else ""
            return text[:200]
    return "passed" if code == 0 else f"failed (exit code {code})"


class Workbench:
    """What the capabilities share: the board, the projects, the worktrees and the settings."""

    def __init__(self, board, registry: Registry, trees: Worktrees, settings: Mapping[str, Any], team=None):
        self.board, self.registry, self.trees, self.settings, self.team = board, registry, trees, settings, team
        self.link = lambda task, tab="": ""  # the page's address for a task, once it is up (set by the module)
        self.patches = trees.root.parent / "patches"  # each task's last change set, kept after its worktree goes

    async def keep_patch(self, task: dict, project) -> None:
        worktree = task["data"].get("worktree")
        if not worktree or not Path(worktree).is_dir():
            return
        change = await self.trees.change(Path(worktree), task["data"]["base"], project.link)
        if not change.empty:
            self.patches.mkdir(parents=True, exist_ok=True)
            (self.patches / f"{task['id']}.diff").write_text(change.patch, encoding="utf-8")

    # ---- finding the task ------------------------------------------------------------------------
    def task(self, arguments: Mapping[str, Any], context) -> tuple[dict, Any]:
        task_id = str(arguments.get("task") or "").strip()
        task = self.board.task(task_id) if task_id else self.board.open_task(str(context.address))
        if task is None:
            raise tools.ToolError(f"No task {task_id}" if task_id else "There is no open task in this conversation")
        if task["address"] != str(context.address):
            raise tools.ToolError("That task belongs to another conversation")
        project = self.registry.get(task["project"])
        if project is None:
            raise tools.ToolError(f"The project {task['project']!r} is not configured")
        return task, project

    def isolation(self, project, folder: Path) -> system.Isolation:
        """What the commands models run in `folder` (and the tests) may reach: the network unless
        `command_network: false`; with `command_filesystem: worktree`, only the system, the folder, the project's
        checkout (read-only) and its git store (a worktree's commits live there)."""
        network = self.settings.get("command_network", True) is not False
        if self.settings.get("command_filesystem", "all") != "worktree":
            return system.Isolation(network=network)
        marker = Path(folder) / ".git"
        found = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
        git_dir = Path(found.split(":", 1)[1].strip()).parents[1] if found.startswith("gitdir:") \
            else Path(project.path) / ".git"
        return system.Isolation(network=network, confined=True, readable=(Path(project.path),), shared=(git_dir,))

    def root(self, task: dict, project, write: bool) -> Path:
        worktree = task["data"].get("worktree")
        if worktree and Path(worktree).is_dir():
            return Path(worktree)
        if write:
            raise tools.ToolError("This task has no worktree yet; changes need one")
        return project.path

    # ---- capability handlers ---------------------------------------------------------------------
    async def open(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        opened = await self.trees.open(project, task["id"])
        self.board.update_task(task["id"], data={"worktree": opened["path"], "branch": opened["branch"],
                                                 "base": opened["base"]})
        return ToolResult.ok(f"Worktree ready on {opened['branch']} from {opened['base'][:10]}", data=opened)

    async def list_files(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = await tools.list_files(self.root(task, project, False), arguments.get("path") or ".",
                                            int(arguments.get("depth") or 3), project.link)
        return ToolResult.ok(text, data=data)

    async def read_file(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = await tools.read_file(self.root(task, project, False), arguments["path"],
                                           arguments.get("start_line"), arguments.get("end_line"), project.link)
        return ToolResult.ok(text, data=data)

    async def search(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = await tools.search(self.root(task, project, False), arguments["pattern"],
                                        arguments.get("path") or ".", bool(arguments.get("ignore_case")), project.link)
        return ToolResult.ok(text, data=data)

    async def write_file(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = tools.write_file(self.root(task, project, True), arguments["path"], arguments["content"],
                                      project.link, bool(arguments.get("append")))
        return ToolResult.ok(text, data=data)

    async def edit_file(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = tools.edit_file(self.root(task, project, True), arguments["path"], arguments["old"],
                                     arguments["new"], bool(arguments.get("replace_all")), project.link)
        return ToolResult.ok(text, data=data)

    async def delete_file(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        text, data = tools.delete_file(self.root(task, project, True), arguments["path"], project.link)
        return ToolResult.ok(text, data=data)

    async def restore(self, arguments, context) -> ToolResult:
        """Put the task's worktree back to an earlier change set of its own (by its tree), e.g. a repair that broke it."""
        task, project = self.task(arguments, context)
        tree = str(arguments.get("tree") or "")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
            raise tools.ToolError("Give the tree of an earlier change set of this task")
        change = await self.trees.restore(Path(self.root(task, project, True)), task["data"]["base"], tree,
                                          project.link)
        return ToolResult.ok(f"Back to the earlier change set: {change.stat()}", data=change.to_dict(patch=False))

    async def run(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        limit = float(self.settings.get("command_timeout_s", 600))
        timeout = max(1.0, min(float(arguments.get("timeout_s") or 120), limit))
        root = self.root(task, project, True)
        text, data = await tools.run(root, arguments["command"], timeout, self.isolation(project, root))
        return ToolResult.ok(text, data=data)

    # ---- the tester's probes: the same line on the old code and the new ------------------------------------
    async def probe_file(self, arguments, context) -> ToolResult:
        """Write an input for probes into the worktree's `.probe/` folder (never part of the change set)."""
        task, project = self.task(arguments, context)
        root = self.root(task, project, True)
        name = str(arguments.get("path") or "").strip().replace("\\", "/")
        if name.startswith(f"{PROBE}/"):
            name = name[len(PROBE) + 1:]
        if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise tools.ToolError("Give a file name inside .probe/ (relative, no ..)")
        target = root / PROBE / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("content") or ""), encoding="utf-8", newline="")
        return ToolResult.ok(f"Wrote {PROBE}/{name} ({target.stat().st_size} bytes); probes see it there.",
                             data={"path": f"{PROBE}/{name}"})

    async def probe(self, arguments, context) -> ToolResult:
        """Run one line in the task's worktree (the new code) and in a pristine copy of its base commit (the old
        code), with the same `.probe/` inputs, and say whether they behave the same."""
        task, project = self.task(arguments, context)
        new = self.root(task, project, True)
        old = await self.trees.base_copy(project, task["id"], task["data"]["base"])
        if (old / PROBE).exists():
            shutil.rmtree(old / PROBE)
        if (new / PROBE).exists():
            shutil.copytree(new / PROBE, old / PROBE)
        timeout = max(1.0, min(float(arguments.get("timeout_s") or 60), float(self.settings.get("command_timeout_s", 600))))
        command = str(arguments.get("command") or "")
        new_text, new_data = await tools.run(new, command, timeout, self.isolation(project, new))
        old_text, old_data = await tools.run(old, command, timeout, self.isolation(project, old))
        same = new_data["exit_code"] == old_data["exit_code"] and \
            comparable(new_data["output"], new) == comparable(old_data["output"], old)  # (paths and timings masked)
        verdict = "The old and the new code behave the same." if same else "The old and the new code behave differently."
        return ToolResult.ok(f"{verdict}\n\n## New code\n{new_text}\n\n## Old code (the base commit)\n{old_text}",
                             data={"same": same, "new_exit": new_data["exit_code"], "old_exit": old_data["exit_code"]})

    async def test(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        root = self.root(task, project, True)
        line = project.test_line()
        if not line:  # none configured or found in the checkout: tests the task itself added count too
            found = detect_test(root)
            line = with_test(project, found).test_line() if found else None
        if not line:
            return ToolResult.fail(f"{project.name} has no tests yet", "no_tests")
        _, data = await tools.run(root, line, float(self.settings.get("test_timeout_s", 900)),
                                  self.isolation(project, root))
        summary = test_summary(data["output"], data["exit_code"]) if not data["timed_out"] else "timed out"
        data = {**data, "summary": summary, "passed": data["exit_code"] == 0, "command": line}
        return ToolResult.ok(f"Tests: {summary}\n$ {line}\n{data['output']}", data=data)

    async def diff(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        worktree = task["data"].get("worktree")
        if not worktree or not Path(worktree).is_dir():
            return ToolResult.ok("No changes (this task has no worktree)", data={"files": [], "stat": "no changes",
                                                                               "fingerprint": "", "patch": ""})
        change = await self.trees.change(Path(worktree), task["data"]["base"], project.link)
        data = change.to_dict(patch=True, limit=80_000)
        files = ()
        if arguments.get("as_file") and not change.empty:
            path = Path(context.workspace) / f"{task['id']}.diff"
            path.write_text(change.patch, encoding="utf-8")
            files = (FileRef(str(path), f"{task['id']}.diff", "text/x-diff", path.stat().st_size),)
        text = change.stat() if change.empty else f"{change.stat()}\n{tools.clip(change.patch, 15_000)}"
        return ToolResult.ok(text, data=data, files=files)

    def tick(self, task: dict, items, checked: bool = True, fingerprint: str = "") -> list[dict]:
        """Tick (or untick) items of the current change set's checklist: their IDs, 1-based numbers, or "all".
        With `fingerprint`, only while it is still the current change set (a box ticked on an older card is not)."""
        current, checklist = views.checklist(self.board, task)
        if not checklist or task["status"] not in CHECKABLE:
            raise tools.ToolError("There is no checklist for this change set yet")
        if fingerprint and fingerprint != current:
            raise tools.ToolError("That checklist is for an older version of the change; look at the latest one")
        fingerprint = current
        wanted = [str(item).strip() for item in (items if isinstance(items, list) else str(items or "").split())]
        chosen = set()
        for item in wanted:
            if item.lower() == "all":
                chosen |= {entry["id"] for entry in checklist}
            elif item.isdigit() and 1 <= int(item) <= len(checklist):
                chosen.add(checklist[int(item) - 1]["id"])
            elif any(entry["id"] == item for entry in checklist):
                chosen.add(item)
            elif item:
                raise tools.ToolError(f"No item {item!r} on the checklist (1-{len(checklist)}, an item ID, or all)")
        ticked = set(((task["data"].get("ticks") or {}).get(fingerprint)) or ())
        ticked = ticked | chosen if checked else ticked - chosen
        self.board.update_task(task["id"], data={"ticks": {fingerprint: sorted(ticked)}})
        return views.checklist(self.board, self.board.task(task["id"]))[1]

    async def check(self, arguments, context) -> ToolResult:
        """Look at the second check, and tick what you looked at (the same ticks as the page's boxes)."""
        words = str(arguments.get("task") or "").split()
        if "items" not in arguments and words and (len(words) > 1 or not words[0].startswith("t-")):
            # typed: `$check t-1a2b 1 2`, or `$check all` in the task's own conversation
            first = words[0] if words and words[0].startswith("t-") else ""
            arguments = {**arguments, "task": first, "items": " ".join(words[1:] if first else words)}
        task, _ = self.task(arguments, context)
        items = arguments.get("items")
        checklist = self.tick(task, items, arguments.get("checked", True) is not False) if items else \
            views.checklist(self.board, task)[1]
        if not checklist:
            return ToolResult.fail("There is no checklist for this change set yet.", "no_checklist")
        lines = [f"{'☑' if entry['ticked'] else '☐'} {number}. {entry['label']}"
                 for number, entry in enumerate(checklist, 1)]
        remaining = sum(not entry["ticked"] for entry in checklist)
        lines.append("Everything is checked: approve the second check." if not remaining else
                     f"{remaining} left: `$check {task['id']} <numbers>` or `all` once you have looked at them.")
        return ToolResult.ok("\n".join(lines), data={"remaining": remaining, "checklist": checklist})

    async def review(self, arguments, context) -> ToolResult:
        task, _ = self.task(arguments, context)
        if task["status"] not in CHECKABLE:
            return ToolResult.fail(f"There is no change set waiting for the second check (the task is {task['status']}).",
                                   "not_in_review")
        fingerprint = str(arguments.get("fingerprint") or (task["data"].get("change") or {}).get("fingerprint") or "")
        current = (task["data"].get("change") or {}).get("fingerprint")
        if not fingerprint or fingerprint != current:
            return ToolResult.fail("This is not the task's current change set; look at the latest one.", "stale")
        listed, checklist = views.checklist(self.board, task)
        if self.settings.get("second_approval", True) and (not checklist or listed != fingerprint):
            return ToolResult.fail("There is no checklist for this change set yet.", "no_checklist")
        missing = [entry for entry in checklist if not entry["ticked"]]
        if missing and self.settings.get("second_approval", True):  # enforced here, whatever the interface
            return ToolResult.fail(
                f"Not yet: {len(missing)} item{'s' if len(missing) > 1 else ''} of the second check "
                f"{'are' if len(missing) > 1 else 'is'} not ticked ({'; '.join(entry['label'] for entry in missing[:4])}"
                f"{' …' if len(missing) > 4 else ''}). Look at each, tick it (the page's boxes, or `$check {task['id']} "
                "all` once you have), then approve again.", "not_checked",
                data={"remaining": [entry["id"] for entry in missing]})
        self.board.update_task(task["id"], status="approved", data={"reviewed": fingerprint})
        self.board.record_outcome(task["id"], "checked", fingerprint=fingerprint)
        return ToolResult.ok(f"Checked: {task['title']} ({(task['data'].get('change') or {}).get('stat', '')})",
                             data={"fingerprint": fingerprint})

    async def land(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        data = task["data"]
        if task["status"] in CLOSED:
            return ToolResult.fail(f"This task is already {task['status']}", "closed")
        if task["status"] in ("failed", "cancelled"):
            return ToolResult.fail("This change set did not pass, so it cannot land. Tell the club what to fix, or "
                                   "discard it.", "not_passed")
        if task["status"] in ("queued", "working"):
            return ToolResult.fail("The club is still working on this task.", "busy")
        if task["status"] not in CHECKABLE:
            return ToolResult.fail(f"There is no change set to land (the task is {task['status']}).", "not_in_review")
        change = data.get("change") or {}
        fingerprint = str(arguments.get("fingerprint") or change.get("fingerprint") or "")
        if not fingerprint or fingerprint != change.get("fingerprint"):
            return ToolResult.fail("This is not the task's current change set.", "stale")
        if self.settings.get("second_approval", True) and data.get("reviewed") != fingerprint:
            return ToolResult.fail("The second check is not done for this change set: tick the checklist in the web "
                                   "page (or type $review), then land it.", "not_checked")
        if not data.get("worktree"):
            return ToolResult.fail("This task has no worktree to land", "no_worktree")
        message = commit_message(task, self.settings)
        await self.keep_patch(task, project)
        try:
            landed = await self.trees.land(project, Path(data["worktree"]), data["branch"], data["base"], message,
                                           fingerprint)
        except (LandError, GitError) as error:
            self.board.record_outcome(task["id"], "land_failed", reason=str(error)[:500])
            return ToolResult.fail(str(error), "land_refused")
        self.board.update_task(task["id"], status="landed", phase="", data={"landed": landed, "worktree": None})
        self.board.record_outcome(task["id"], "landed", commit=landed["commit"], mode=landed["mode"],
                                  totals=self.board.totals(task["id"]))
        where = (f"{landed['branch']} as {landed['commit'][:8]}" if landed["mode"] == "commit"
                 else f"branch {landed['branch']} ({landed['commit'][:8]})")
        return ToolResult.ok(f"Landed {task['title']} on {where}: {landed['stat']}", data=landed)

    async def discard(self, arguments, context) -> ToolResult:
        task, project = self.task(arguments, context)
        if task["status"] == "landed":
            return ToolResult.fail("This task has landed already; revert its commit to undo it", "landed")
        if task["status"] == "discarded":
            return ToolResult.fail("This task is discarded already", "closed")
        if task["status"] == "working":  # its worktree is in use: stop the run first
            return ToolResult.fail("The club is working on this task right now: stop the run first ($cancel), then "
                                   "discard it.", "busy")
        data = task["data"]
        if data.get("worktree"):
            await self.keep_patch(task, project)
            await self.trees.remove(project, Path(data["worktree"]), data.get("branch", f"workbench/{task['id']}"))
        self.board.update_task(task["id"], status="discarded", phase="", data={"worktree": None})
        self.board.record_outcome(task["id"], "discarded")
        return ToolResult.ok(f"Discarded {task['title']}; its worktree and branch are gone.")

    async def tasks(self, arguments, context) -> ToolResult:
        """The Logbook, for anyone the creator talks with (MomoChat remembers the Classroom through it)."""
        limit = max(1, min(int(arguments.get("limit") or 10), 50))
        rows = self.board.search(str(arguments.get("query") or ""), str(arguments.get("project") or ""), limit)
        if not rows:
            return ToolResult.ok("Nothing in the Logbook matches." if arguments.get("query") else "No tasks yet.")
        lines = []
        for row in rows:
            data = row["data"]
            line = f"{row['id']} · {row['status']} · {row['project']} · {row['title']}"
            if (data.get("change") or {}).get("stat"):
                line += f" · {data['change']['stat']}"
            if data.get("landed"):
                line += f" · landed as {str(data['landed'].get('commit', ''))[:8]} on {data['landed'].get('branch', '')}"
            line += f" · {time.strftime('%Y-%m-%d', time.localtime(row['updated']))}"
            summary = str(data.get("summary") or data.get("answer") or "").strip()
            if summary:
                line += "\n  " + " ".join(summary.split())[:400]
            lines.append(line)
        return ToolResult.ok("\n".join(lines), data={"tasks": [row["id"] for row in rows]})

    async def invite(self, arguments, context) -> ToolResult:
        """An invitation to the Classroom: a task waiting for the person, with the conversation's latest part, the
        classmate's brief and her notes. It starts nothing; following the link (and starting it there) does."""
        if context.address.interface == str(self.settings.get("interface", "workbench")):
            return ToolResult.fail("You are in the Classroom already.", "here_already")
        projects = self.registry.all()
        project = str(arguments.get("project") or "").strip().lower()
        if project and project not in projects:
            return ToolResult.fail(f"There is no project {project!r}. The projects: {', '.join(sorted(projects))}.",
                                   "unknown_project")
        if not project:
            real = [name for name, item in projects.items() if item.source != "sample"]
            default = self.settings.get("default_project")
            project = (default if default in projects else real[0] if len(real) == 1
                       else "sample" if not real and "sample" in projects else "")  # else: picked on arrival
        brief = str(arguments.get("brief") or "").strip()
        if not brief:
            return ToolResult.fail("Say in the brief what the club should do in the Classroom.", "no_brief")
        who = str(arguments.get("classmate") or "").strip()
        lead = self.team.seat_for(who) if self.team is not None else None
        crew, unknown, keyless = [], [], []
        for name in list(arguments.get("companions") or ())[:6]:  # classmates she asks along
            seat = self.team.seat_for(str(name)) if self.team is not None else None
            if seat is None:
                unknown.append(str(name))
            elif not self._has_key(seat, context):  # no model key here: she could not do anything
                keyless.append(seat.name)
            elif seat.persona not in crew and (lead is None or seat.persona != lead.persona):
                crew.append(seat.persona)
        task_id = new_id()
        title = str(arguments.get("title") or "").strip() or brief.splitlines()[0][:80]
        self.board.create_task(task_id, f"{self.settings.get('interface', 'workbench')}:{task_id}", project, title,
                               request=brief, invited={"by": who, "lead": lead.persona if lead else "", "crew": crew,
                                                       "from": str(context.address), "notes": str(arguments.get("notes")
                                                                                                  or "").strip(),
                                                       "excerpt": self._excerpt(context), "at": time.time()},
                               origin_address=str(context.address))
        self.board.update_task(task_id, status="invited")
        self.board.record_outcome(task_id, "invited", by=who, origin=str(context.address))
        link = self.link(task_id, "")
        where = f" in {project}" if project else ""
        company = f" Coming along: {', '.join(self.team.seats[name].name for name in crew)}." if crew else ""
        missing = (f" Not in the Classroom's team, so not coming: {', '.join(unknown)}." if unknown else "") + (
            f" No model key here, so they can't come: {', '.join(keyless)}." if keyless else "")
        return ToolResult.ok(f"Invitation to the Classroom ready{where}: {link or task_id}.{company}{missing} Give the "
                             "person this link in your next message so they can follow you; nothing starts until they "
                             "do.",
                             data={"task": task_id, "link": link, "project": project, "crew": crew})

    def _has_key(self, seat, context) -> bool:
        """Whether the seat's models can answer: asked of the core (Models.ready), else what the last run saw."""
        asking = getattr(getattr(context, "models", None), "ready", None)
        if callable(asking):
            return bool(asking(seat.profile("cheap")))
        return self.team.ready.get(seat.persona) is not False

    def _excerpt(self, context) -> list[dict]:
        """The latest part of the calling conversation (commands left out): what moves to the Classroom."""
        history = getattr(context, "history", None)
        if history is None:
            return []
        limit = max(2, min(int(self.settings.get("bring_messages", 16)), 60))
        items = [item for item in history.recent(limit + 10)
                 if not (item.direction == "in" and item.text.strip()[:1] == "$")][-limit:]
        return [{"who": item.sender_name or ("you" if item.direction == "in" else "the club"), "text": item.text[:2000],
                 "at": item.at, "mine": item.direction == "in"} for item in items]

    async def projects(self, arguments, context) -> ToolResult:
        lines = [f"{project.name} · {project.path} · tests: {project.test or 'none found'}"
                 + (" (guessed)" if project.detected and project.test else "")
                 for project in self.registry.all().values()]
        return ToolResult.ok("\n".join(lines) or "No projects.", data={"projects": sorted(self.registry.all())})

    # ---- approval prompts and tiers ---------------------------------------------------------------
    def command_tier(self, arguments: Mapping[str, Any]) -> str | None:
        """Off the allowlist, a shell line is critical (it asks); `commands: any` runs everything, `ask` asks always."""
        mode = str(self.settings.get("commands", "allowlist"))
        if mode == "any":
            return None
        if mode == "ask":
            return "critical"
        extra = list(self.settings.get("allow") or ())
        task = self.board.task(str(arguments.get("task") or ""))
        project = self.registry.get(task["project"]) if task else None
        if project and project.test_line():
            extra.append(project.test_line())
        return None if allowlist.allowed(str(arguments.get("command") or ""), extra) else "critical"

    def describe(self, what: str):
        def line(arguments: Mapping[str, Any]) -> str:
            task = self.board.task(str(arguments.get("task") or "")) or {}
            title = task.get("title", "this task")
            change = (task.get("data") or {}).get("change") or {}
            if what == "run":
                return f"Run in the worktree of “{title}”: {arguments.get('command')}"
            if what == "probe":
                return f"Run on the new code of “{title}” and on its base commit: {arguments.get('command')}"
            if what == "review":
                count = len(arguments.get("checklist") or ())
                return (f"Second check of “{title}” ({change.get('stat', '')}): look at all {count} items on the "
                        "checklist, then approve. The final approval to land it comes next.")
            if what == "land":
                project = self.registry.get(task.get("project", ""))
                where = "its own branch" if project and project.land == "branch" else "your current branch"
                return f"Land “{title}” in {task.get('project', '?')} on {where}: {change.get('stat', '')}"
            return f"{what} {title}"
        return line


def commit_message(task: dict, settings: Mapping[str, Any]) -> str:
    data = task["data"]
    review = data.get("review") or {}
    route = data.get("route") or {}
    people = [f"drafted by {data.get('finished_by') or '?'}"]
    if review.get("seat"):
        people.append(f"reviewed by {review['seat']}")
    summary = str(data.get("summary") or "").strip()
    body = f"\n\n{summary[:3000]}" if summary else ""
    return (f"{task['title']}{body}\n\nWorkbench task {task['id']} · {route.get('workflow', task.get('workflow') or '?')}"
            f" · {', '.join(people)}")


def guarded(handler):
    """Problems the model can fix (a wrong path, text that is not there, a git refusal) come back as failures it reads."""
    async def run(arguments, context) -> ToolResult:
        try:
            return await handler(arguments, context)
        except tools.ToolError as error:
            return ToolResult.fail(str(error), "invalid")
        except GitError as error:
            return ToolResult.fail(str(error), "git_failed")
        except (OSError, UnicodeError) as error:
            return ToolResult.fail(f"{type(error).__name__}: {error}", "os_error")
    return run


def build(workbench: Workbench) -> list[Capability]:
    w = workbench
    path = {"path": {"type": "string", "description": "a path relative to the project root"}}
    common = dict(access="creator")
    task_option = Option("task", "Task ID (default: this conversation's open task)")
    return [
        Capability("workbench.open", "Make the task's worktree.", guarded(w.open), {**TASK, "project": {"type": "string"}},
                   ("task",), Risk.PREPARE, internal=True, timeout_s=300, **common),
        Capability("workbench.restore", "Put the task's worktree back to an earlier change set of its own.",
                   guarded(w.restore), {**TASK, "tree": {"type": "string"}}, ("task", "tree"), Risk.PREPARE,
                   internal=True, timeout_s=120, **common),
        Capability("workbench.list_files", "List the project's files.", guarded(w.list_files),
                   {**TASK, **path, "depth": {"type": "integer"}}, ("task",), Risk.READ, internal=True, **common),
        Capability("workbench.read_file", "Read a file of the project.", guarded(w.read_file),
                   {**TASK, **path, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
                   ("task", "path"), Risk.READ, internal=True, subject="path", subject_kind="path", **common),
        Capability("workbench.search", "Search the project's files.", guarded(w.search),
                   {**TASK, "pattern": {"type": "string"}, **path, "ignore_case": {"type": "boolean"}},
                   ("task", "pattern"), Risk.READ, internal=True, **common),
        Capability("workbench.write_file", "Write a file in the task's worktree.", guarded(w.write_file),
                   {**TASK, **path, "content": {"type": "string"}, "append": {"type": "boolean"}},
                   ("task", "path", "content"), Risk.PREPARE,
                   internal=True, subject="path", subject_kind="path", **common),
        Capability("workbench.edit_file", "Replace text in a file of the task's worktree.", guarded(w.edit_file),
                   {**TASK, **path, "old": {"type": "string"}, "new": {"type": "string"},
                    "replace_all": {"type": "boolean"}}, ("task", "path", "old", "new"), Risk.PREPARE,
                   internal=True, subject="path", subject_kind="path", **common),
        Capability("workbench.delete_file", "Delete a file in the task's worktree.", guarded(w.delete_file),
                   {**TASK, **path}, ("task", "path"), Risk.PREPARE, internal=True, subject="path",
                   subject_kind="path", **common),
        Capability("workbench.run", "Run a shell line in the task's worktree.", guarded(w.run),
                   {**TASK, "command": {"type": "string"}, "timeout_s": {"type": "number"}}, ("task", "command"),
                   Risk.PREPARE, internal=True, subject="command", grantable=True, escalate=w.command_tier,
                   describe=w.describe("run"), timeout_s=1800, **common),
        Capability("workbench.probe_file", "Write a probe input into the worktree's .probe/ folder.",
                   guarded(w.probe_file), {**TASK, "path": {"type": "string"}, "content": {"type": "string"}},
                   ("task", "path"), Risk.PREPARE, internal=True, **common),
        Capability("workbench.probe", "Run one line on the new code and on the base commit; compare.", guarded(w.probe),
                   {**TASK, "command": {"type": "string"}, "timeout_s": {"type": "number"}}, ("task", "command"),
                   Risk.PREPARE, internal=True, subject="command", grantable=True, escalate=w.command_tier,
                   describe=w.describe("probe"), timeout_s=900, **common),
        Capability("workbench.test", "Run the project's tests in the task's worktree.", guarded(w.test), dict(TASK),
                   ("task",), Risk.PREPARE, internal=True, timeout_s=1800, **common),
        Capability("workbench.diff", "The task's change set.", guarded(w.diff), {**TASK, "as_file": {"type": "boolean"}},
                   ("task",), Risk.READ, internal=True, timeout_s=120, **common),
        Capability("workbench.check", "The second check's checklist: show it, and tick the items you looked at. items: "
                   "\"all\", item numbers or IDs; checked: false unticks.", guarded(w.check),
                   {**TASK, "items": {"type": ["string", "array"]}, "checked": {"type": "boolean"}}, (), Risk.READ,
                   internal=True, command=Command("check", "Show the second check, or tick what you looked at",
                                                  (task_option, Option("items", "all, or item numbers")),
                                                  template="[{task}] [{items}]"), command_argument="task", **common),
        Capability("workbench.review", "The second check: confirm you looked at every item of the change set.", guarded(w.review), {**TASK, "fingerprint": {"type": "string"}, "checklist": {
                       "type": "array", "items": {"type": "object", "properties": {
                           "id": {"type": "string"}, "label": {"type": "string"}},
                           "additionalProperties": False}}},
                   (), Risk.WRITE, never_auto=True, internal=True, describe=w.describe("review"),
                   command=Command("review", "Mark this conversation's change set as checked (the second check)",
                                   (task_option,), template="[{task}]"), command_argument="task", **common),
        Capability("workbench.land", "The final approval: land the change set as one commit.", guarded(w.land),
                   {**TASK, "fingerprint": {"type": "string"}}, (), Risk.CRITICAL, never_auto=True, confirm=True,
                   internal=True,
                   describe=w.describe("land"), timeout_s=600,
                   command=Command("land", "Land this conversation's checked change set (asks to confirm)",
                                   (task_option,), template="[{task}]"), command_argument="task", **common),
        Capability("workbench.discard", "Throw a task's change set away (its worktree and branch).", guarded(w.discard),
                   dict(TASK), (), Risk.WRITE, internal=True,
                   command=Command("discard", "Discard this conversation's open task", (task_option,),
                                   template="[{task}]"), command_argument="task", **common),
        Capability("workbench.tasks", "The Logbook: what the club did in the Classroom (tasks with their status, "
                   "outcome and summary), newest first. query: words to look for; project: one project's tasks.",
                   guarded(w.tasks), {"query": {"type": "string"}, "project": {"type": "string"},
                                      "limit": {"type": "integer"}},
                   (), Risk.READ, command=Command("tasks", "The Logbook: recent tasks, or those matching words",
                                                  (Option("query", "Words to look for"),), template="[{query}]"),
                   command_argument="query", **common),
        Capability("workbench.invite",
                   "Take the person to the Classroom, where the club changes code in one of their projects (fix, add, "
                   "build, refactor, test). Use it when they want such work done, not for what you can answer here. "
                   "Ask in your message whether they want to come along (for example: 'I think we need to go to the "
                   "Classroom for this, do you want to come with me?'). It only prepares an invitation and returns a "
                   "link to give them; nothing starts until they follow it. brief: what the club should do there, with "
                   "every decision, name and example from this conversation; notes: what you know about how they "
                   "like such work done; project: one of workbench.projects if known; title: a short name; classmate: your name; "
                   "companions: classmates you would like to bring along (names), when the work wants more hands or "
                   "another view; the person can change who comes.",
                   guarded(w.invite), {"brief": {"type": "string"}, "title": {"type": "string"},
                                       "project": {"type": "string"}, "notes": {"type": "string"},
                                       "classmate": {"type": "string"},
                                       "companions": {"type": "array", "items": {"type": "string"}}},
                   ("brief",), Risk.PREPARE, **common),
        Capability("workbench.projects", "List the projects the club may work in.", guarded(w.projects), {}, (), Risk.READ,
                   command="projects", **common),
    ]

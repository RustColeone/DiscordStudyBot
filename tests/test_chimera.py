"""The chimera: its workspace (worktrees, tools, the allowlist), its workflows in the real core with scripted
models, its page, and the same module inside the real core."""

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiohttp

from contracts import AdapterResult, Address, Request, Sender
from tests.core_support import FIXTURES, ROOT, iface, settle, start_core

from extensions.chimera.extension import assemble
from core.app import Core
from core.config import CoreConfig
from extensions.chimera.demo import DemoAdapter
from extensions.chimera.workspace import commands, tools
from extensions.chimera.workspace.projects import detect_test, make_sample, project_from
from extensions.chimera.workspace.worktree import LandError, Worktrees

# The providers the students and the core bring (each answered by a scripted adapter in the tests).
PROVIDERS = ("deepseek", "openai", "gemini", "claude", "grok")
SUNDAY = "Meetings on Sunday are listed first instead of last. Fix it and add a test."
TEAM = {  # the scripted club: two families, so reviewers come from another one
    "team": {"gpt": False, "claude": False, "gemini": False, "grok": False, "deepseek": False,
             "drafty": {"name": "Drafty", "family": "alpha", "cheap": ["alpha/a-small"], "strong": ["alpha/a-large"]},
             "checky": {"name": "Checky", "family": "beta", "cheap": ["beta/b-small"], "strong": ["beta/b-large"]}},
    "roles": {"router": ["checky"], "drafter": ["drafty", "checky"], "judge": ["checky", "drafty"],
              "strong": ["checky", "drafty"], "critic": ["checky", "drafty"]},
    "providers": {"alpha": {"kind": "openai_compatible"}, "beta": {"kind": "openai_compatible"}},
}


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


def turn(*actions, status="continue", say="", summary="", note="working"):
    return {"say": say, "note": note, "plan": [], "status": status, "summary": summary,
            "actions": [{"tool": tool, "arguments": arguments} for tool, arguments in actions]}


class Scripted:
    """A model that answers by `answer(kind, system, messages, turns)`; kind is route, turn, review or chat."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        kind = {"chimera_route": "route", "chimera_turn": "turn", "chimera_review": "review",
                "chimera_requirements": "requirements"}.get(schema_name, "chat")
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        turns = sum("Results of your actions" in message["content"] for message in messages)
        value = self.answer(kind, system, messages, turns)
        self.calls.append((kind, model, effort))
        return AdapterResult(value if isinstance(value, str) else json.dumps(value), 120, 40)


def route(kind="change", size="small", risk="low"):
    return {"kind": kind, "size": size, "risk": risk, "title": "A task", "reason": "scripted"}


class Club:
    """The Classroom in the real core, with only the chimera module and the students (as `python -m extensions.chimera`
    runs it), in a temporary folder; every provider answered by the scripted `adapter`."""

    def __init__(self, tmp: Path, adapter, **config):
        self.tmp, self.adapter, self.config = tmp, adapter, config
        self.count = 0

    async def start(self):
        # Verification (criteria, the tester) is off unless a test turns it on: the scripted models play the rest.
        lab = {"port": 0, "host": "127.0.0.1", "data_dir": str(self.tmp / "data"),
               "verify": {"requirements": False, "probe": False}, **self.config}
        raw = {"data_dir": str(self.tmp / "data" / "core"), "env_files": [],
               "modules": {"dirs": [], "paths": [str(ROOT / "extensions" / "students"),
                                                 str(ROOT / "extensions" / "chimera")]},
               "extension_config": {"chimera": lab, "students": {"learn": False}}}
        names = {*PROVIDERS, *dict(self.config.get("providers") or {})}
        self.core = Core(CoreConfig(raw, self.tmp), provider_adapters={name: self.adapter for name in names},
                         tick_seconds=3600)
        await self.core.start()
        self.page = iface(self.core, "classroom")
        self.board = self.page.board
        self.parts = {"registry": self.page.registry, "board": self.board, "page": self.page}
        return self

    async def stop(self):
        await self.core.stop()

    async def idle(self):
        """Until no run or command is being carried out (approvals resume runs, which may start more)."""
        for _ in range(400):
            await settle(self.core)
            await asyncio.sleep(0.01)
            if not self.core.manager._tasks and not self.core.gateway._tasks:
                return

    async def submit(self, request):
        return await self.page.port.submit(request)

    async def say(self, task, text, sender=("creator", "Leo"), extra=None):
        self.count += 1
        await self.submit(Request(id=f"m{self.count}-{time.time_ns()}", address=Address("classroom", task),
                                  sender=Sender(*sender), text=text, received_at=time.time(),
                                  display=self.page.display, extra={"task": task, **(extra or {})}))
        await asyncio.sleep(0.05)
        await self.idle()

    async def new(self, text, project="sample", workflow=None, crew=None):
        task = f"t-test{self.count:04d}{time.time_ns() % 100000}"
        self.board.create_task(task, f"classroom:{task}", project, text[:60], request=text,
                               **({"crew": crew} if crew else {}))
        await self.say(task, text, extra={"project": project, **({"workflow": workflow} if workflow else {})})
        return task

    def approvals(self, task, capability=None):
        return [item for item in self.board.messages(task) if item.get("code") == "approval_request"
                and (capability is None or item["data"]["capability"] == capability)]

    async def decide(self, task, capability, approve=True):
        if capability == "chimera.review" and approve:
            await self.say(task, f"$check {task} all")  # looked at everything, as the second check asks
        approval = self.approvals(task, capability)[-1]["data"]["approval_id"]
        await self.say(task, f"${'approve' if approve else 'deny'} {approval}")
        await asyncio.sleep(0.2)
        await self.idle()

    def task(self, task):
        return self.board.task(task)

    def said(self, task):
        return [(item.get("speaker"), item["text"]) for item in self.board.messages(task) if item.get("kind") == "text"]

    @property
    def sample(self):
        return self.tmp / "data" / "sample"


# ---- the workspace ---------------------------------------------------------------------------------------------
class AllowlistTests(unittest.TestCase):
    def test_tests_and_looking_around_run_alone(self):
        for line in ["pytest -q", "python3 -m unittest discover -s tests -t .", "venv/bin/python -m pytest",
                     "npm test", "pytest -q 2>&1 | tail -20", "git diff --stat", "FOO=1 pytest", "cat src/a.py"]:
            self.assertTrue(commands.allowed(line), line)

    def test_changes_secrets_and_paths_outside_ask(self):
        for line in ["rm -rf build", "git commit -am x", "sed -i s/a/b/ f", "echo $(whoami)", "pip install x",
                     "cat ../../.env", "cat /etc/passwd", "cat .env", "ls ~", "env", "curl http://example.com"]:
            self.assertFalse(commands.allowed(line), line)

    def test_separators_inside_quotes_do_not_split_a_line(self):
        self.assertEqual(commands.pieces('python3 -c "from x import y; print(y)" | head'),
                         ['python3 -c "from x import y; print(y)"', "head"])
        self.assertTrue(commands.allowed('python3 -c "from clubnotes import schedule; print(schedule)"'))
        self.assertFalse(commands.allowed('python3 -c "print(1)"; rm -rf build'))  # outside the quotes it splits
        self.assertFalse(commands.allowed("echo 'a' && cat ../secret"))

    def test_lines_that_once_slipped_through_now_ask(self):
        for line in ["npm test --silent && id", "npm test --silent $(id)", 'echo \\" ; id ; echo \\"',
                     "echo hi >../out.txt", "echo >>/abs/file", "ls 2>/abs", "echo &>../f", "sort -o../out.txt a",
                     "git -C.. log", "git branch -D main", "git branch -f x", "git branch new", "cat ${HOME}/x",
                     "cat $HOME/x", "ls | xargs id", "uv run id", "find . -delete", "find . -exec rm {} +",
                     "sed 'w out' a", "awk '{system(\"id\")}' a", "cat C:\\\\Windows\\\\x", "cat ..\\\\secret",
                     "cat <<EOF", "timeout 5 rm x", "env rm -rf x", "sleep 1 & rm -rf x"]:
            self.assertFalse(commands.allowed(line, ["npm test --silent"]), line)

    def test_everyday_lines_run_without_asking(self):
        for line in ["cd sub && pytest", "git -C sub status", "npx jest", "timeout 60 pytest", "uv run pytest",
                     "source venv/bin/activate && pytest", "git branch -a", 'echo "exit=$?"', "sed -n 1,20p a.py",
                     "awk '{print $1}' a.txt", "find . -name '*.py'", "pytest -q > out.txt", "npm test --silent",
                     "python3 -m clubnotes conflicts .probe/x.txt; echo exit=$?"]:
            self.assertTrue(commands.allowed(line, ["npm test --silent"]), line)

    def test_a_projects_own_test_line_is_allowed_as_written(self):
        self.assertTrue(commands.allowed("/abs/venv/bin/python -m unittest", ["/abs/venv/bin/python -m unittest"]))


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = make_sample(self.tmp / "sample")
        self.project = project_from("sample", {"path": str(self.repo)})
        self.trees = Worktrees(self.tmp / "worktrees")

    def tearDown(self):
        self._tmp.cleanup()

    def test_test_lines_are_guessed_from_the_files(self):
        self.assertEqual(detect_test(self.repo), "{python} -m unittest discover -s tests -t .")
        (self.tmp / "node").mkdir()
        (self.tmp / "node" / "package.json").write_text('{"scripts": {"test": "jest"}}')
        self.assertEqual(detect_test(self.tmp / "node"), "npm test --silent")
        self.assertIsNone(detect_test(self.tmp))

    def test_worktrees_live_where_configured_even_from_a_relative_path(self):
        self.assertTrue(Worktrees(Path("relative/worktrees")).root.is_absolute())

    def test_paths_stay_inside_the_folder(self):
        for path in ("../x", "/etc/passwd", ".git/config"):
            with self.assertRaises(tools.ToolError):
                tools.inside(self.repo, path)
        self.assertEqual(tools.inside(self.repo, "clubnotes/schedule.py"), (self.repo / "clubnotes/schedule.py").resolve())

    def test_edits_need_exact_unique_text(self):
        with self.assertRaisesRegex(tools.ToolError, "not found"):
            tools.edit_file(self.repo, "clubnotes/schedule.py", "no such text", "x")
        with self.assertRaisesRegex(tools.ToolError, "occurs"):
            tools.edit_file(self.repo, "clubnotes/schedule.py", "import", "x")

    async def test_commands_run_without_the_model_keys(self):
        import os
        os.environ["SOMETHING_API_KEY"] = "sk-not-for-models"
        try:
            _, data = await tools.run(self.repo, "env")
        finally:
            del os.environ["SOMETHING_API_KEY"]
        self.assertNotIn("sk-not-for-models", data["output"])

    async def open_and_edit(self, task="t-1"):
        opened = await self.trees.open(self.project, task)
        tools.edit_file(Path(opened["path"]), "clubnotes/schedule.py", "return ((DAYS.index(self.day) + 1) % 7, self.time)",
                        "return (DAYS.index(self.day), self.time)")
        tools.write_file(Path(opened["path"]), "NOTES.md", "new\n")
        return opened, await self.trees.change(Path(opened["path"]), opened["base"])

    async def test_a_change_set_lands_as_one_fast_forward_commit(self):
        opened, change = await self.open_and_edit()
        self.assertEqual(change.stat(), "2 files (+2 −1)")
        self.assertEqual({item["path"] for item in change.files}, {"clubnotes/schedule.py", "NOTES.md"})
        with self.assertRaisesRegex(LandError, "different"):
            await self.trees.land(self.project, Path(opened["path"]), opened["branch"], opened["base"], "x", "stale")
        landed = await self.trees.land(self.project, Path(opened["path"]), opened["branch"], opened["base"],
                                       "Sort Sunday last", change.fingerprint)
        self.assertTrue(landed["fast_forward"])
        self.assertEqual(git(self.repo, "log", "-1", "--format=%s"), "Sort Sunday last")
        self.assertFalse(Path(opened["path"]).exists())
        self.assertNotIn("chimera/t-1", git(self.repo, "branch"))

    async def test_landing_cherry_picks_onto_a_branch_that_moved(self):
        opened, change = await self.open_and_edit()
        (self.repo / "OTHER.md").write_text("meanwhile\n")
        git(self.repo, "add", "OTHER.md")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Meanwhile")
        landed = await self.trees.land(self.project, Path(opened["path"]), opened["branch"], opened["base"],
                                       "Sort Sunday last", change.fingerprint)
        self.assertFalse(landed["fast_forward"])
        self.assertEqual(git(self.repo, "log", "--format=%s", "-2").splitlines(), ["Sort Sunday last", "Meanwhile"])

    async def test_landing_stops_before_touching_a_dirty_file_or_a_conflict(self):
        opened, change = await self.open_and_edit()
        (self.repo / "NOTES.md").write_text("mine\n")
        with self.assertRaisesRegex(LandError, "uncommitted"):
            await self.trees.land(self.project, Path(opened["path"]), opened["branch"], opened["base"], "x",
                                  change.fingerprint)
        (self.repo / "NOTES.md").unlink()
        schedule = self.repo / "clubnotes/schedule.py"
        schedule.write_text(schedule.read_text().replace("% 7, self.time)", "% 7, self.time)  # theirs"))
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "Theirs")
        before = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(LandError, "conflicts"):
            await self.trees.land(self.project, Path(opened["path"]), opened["branch"], opened["base"], "x",
                                  change.fingerprint)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        again = await self.trees.change(Path(opened["path"]), opened["base"])
        self.assertEqual(again.fingerprint, change.fingerprint)  # the worktree keeps the change to try again


# ---- prices ----------------------------------------------------------------------------------------------------
def aa_model(slug, creator, price_in, price_out, name=None):
    return {"id": slug, "name": name or slug, "slug": slug, "release_date": "2026-09-01",
            "model_creator": {"id": creator.lower(), "name": creator},
            "pricing": {"price_1m_input_tokens": price_in, "price_1m_output_tokens": price_out,
                        "price_1m_cache_hit_tokens": None, "price_1m_cache_write_tokens": None}}


class PriceTests(unittest.IsolatedAsyncioTestCase):
    """The price list against a stand-in for Artificial Analysis's free models list."""

    PAGES = [[aa_model("deepseek-v4-flash", "DeepSeek", 0.1, 0.4), aa_model("deepseek-v4-pro", "DeepSeek", 0.5, 2.0),
              aa_model("claude-5-5-sonnet", "Anthropic", 3, 15)],
             [aa_model("gemini-3-8-flash", "Google", 0.2, 0.8), aa_model("gemini-3-6-flash", "Google", 0.2, 0.6),
              aa_model("gemini-3-8-flash-lite", "Google", 0.05, 0.2),
              aa_model("grok-4-fast", "xAI", 0.2, 0.5), aa_model("grok-4-fast-reasoning", "xAI", 0.2, 0.5),
              aa_model("unmeasured", "Someone", None, None)]]

    async def asyncSetUp(self):
        from aiohttp import web
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.requests, self.down = [], False

        async def models(request):
            self.requests.append((request.headers.get("x-api-key"), request.query.get("page")))
            if self.down:
                return web.json_response({"error": "down"}, status=503)
            page = int(request.query.get("page", "1"))
            return web.json_response({"tier": "free", "data": self.PAGES[page - 1], "pagination": {
                "page": page, "page_size": 3, "total_pages": 2, "has_more": page < 2}})
        app = web.Application()
        app.router.add_get("/free", models)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/free"
        self.now = 1_800_000_000.0
        os.environ["TEST_AA_KEY"] = "aa-test-key"

    async def asyncTearDown(self):
        import os
        os.environ.pop("TEST_AA_KEY", None)
        await self.runner.cleanup()
        self._tmp.cleanup()

    def book(self, **settings):
        from extensions.chimera.prices import PriceBook
        return PriceBook(self.tmp / "prices.json", {"prices_url": self.url, "prices_key_env": "TEST_AA_KEY", **settings},
                         clock=lambda: self.now)

    async def test_the_list_is_fetched_on_use_once_a_day_old(self):
        book = self.book()
        self.assertEqual(await book.refresh_if_stale(), "fetched")
        self.assertEqual(self.requests, [("aa-test-key", "1"), ("aa-test-key", "2")])  # every page
        self.assertEqual(len(book.cache["models"]), 8)  # an unmeasured price is left out, never taken as free
        self.now += 23 * 3600
        self.assertEqual(await book.refresh_if_stale(), "fresh")
        self.assertEqual(len(self.requests), 2)
        self.now += 2 * 3600
        self.assertEqual(await self.book().refresh_if_stale(), "fetched")  # kept on disk, so a new book knows its age
        self.assertEqual(len(self.requests), 4)

    async def test_a_failed_fetch_keeps_the_last_list_and_waits_an_hour(self):
        book = self.book()
        await book.refresh_if_stale()
        self.now += 25 * 3600
        self.down = True
        self.assertEqual(await book.refresh_if_stale(), "failed")
        self.assertEqual(book.price("deepseek", "deepseek-v4-pro")["input"], 0.5)  # the old list still prices
        self.assertEqual(await book.refresh_if_stale(), "waiting")
        self.now += 3601
        self.down = False
        self.assertEqual(await book.refresh_if_stale(), "fetched")
        self.assertIsNone(book.cache["error"])

    async def test_without_a_key_nothing_is_asked(self):
        import os
        os.environ.pop("TEST_AA_KEY")
        self.assertEqual(await self.book().refresh_if_stale(), "no_key")
        self.assertEqual(self.requests, [])

    async def test_models_are_matched_by_name_and_never_guessed(self):
        book = self.book(price_aliases={"gemini/gemini-fast": "gemini-3-8-flash-lite"},
                         prices={"claude/claude-opus-5-5": {"input": 9, "output": 45}})
        await book.refresh_if_stale()
        self.assertEqual(book.price("deepseek", "deepseek-v4-pro")["slug"], "deepseek-v4-pro")       # exact
        self.assertEqual(book.price("claude", "claude-sonnet-5-5")["slug"], "claude-5-5-sonnet")     # words reordered
        self.assertEqual(book.price("deepseek", "deepseek-flash")["slug"], "deepseek-v4-flash")      # the one closest
        self.assertEqual(book.price("gemini", "gemini-3.8-flash")["slug"], "gemini-3-8-flash")       # dots are dashes
        self.assertEqual(book.price("grok", "grok-4-fast")["slug"], "grok-4-fast")
        self.assertIsNone(book.price("gemini", "gemini-flash"))      # 3.8 and 3.6 equally close: no guess
        self.assertEqual(book.price("gemini", "gemini-fast")["slug"], "gemini-3-8-flash-lite")       # an alias
        self.assertEqual(book.price("claude", "claude-opus-5-5")["source"], "config")                # yours win
        self.assertIsNone(book.price("openrouter", "deepseek-v4-pro-x"))
        self.assertEqual(book.cost(book.price("deepseek", "deepseek-v4-pro"), 1_000_000, 500_000), 1.5)
        status = book.status()
        self.assertEqual((status["attribution"]["name"], status["count"]), ("Artificial Analysis", 8))
        self.assertEqual([entry["slug"] for _, entry in book.candidates("gemini", "gemini-flash", 2)],
                         ["gemini-3-8-flash", "gemini-3-6-flash"])  # shown on the page, to pick an alias from


# ---- workflows in the real core ---------------------------------------------------------------------------------
class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clubs = []

    async def asyncTearDown(self):
        for club in self.clubs:
            await club.stop()
        self._tmp.cleanup()

    async def club(self, adapter, **config):
        club = await Club(self.tmp, adapter, **config).start()
        self.clubs.append(club)
        return club

    async def test_a_cascade_escalates_failing_tests_and_lands_after_two_approvals(self):
        club = await self.club(DemoAdapter())
        task = await club.new(SUNDAY)
        state = club.task(task)
        self.assertEqual((state["status"], state["workflow"]), ("review", "cascade"))
        kinds = [step["id"] for step in club.board.steps(task)]
        self.assertIn("r1.draft.test", kinds)
        self.assertIn("r1.escalate.1", kinds)
        self.assertEqual([item["data"]["capability"] for item in club.approvals(task)], ["chimera.review"])
        self.assertEqual(git(club.sample, "rev-list", "--count", "HEAD"), "1")  # nothing landed yet
        await club.decide(task, "chimera.review")
        self.assertEqual(club.task(task)["status"], "approved")
        await club.decide(task, "chimera.land")
        self.assertEqual(club.task(task)["status"], "landed")
        self.assertEqual(git(club.sample, "rev-list", "--count", "HEAD"), "2")
        listing = subprocess.run(["python3", "-m", "clubnotes", "list", "schedule.txt"], cwd=club.sample,
                                 capture_output=True, text=True).stdout.splitlines()
        self.assertTrue(listing[-1].startswith("Sun"), listing)
        events = [json.loads(line)["event"] for line in (club.tmp / "data" / "outcomes.jsonl").read_text().splitlines()]
        self.assertEqual(events, ["routed", "escalated", "ready", "checked", "landed"])
        totals = club.board.totals(task)
        self.assertGreater(totals["calls"], 4)
        self.assertIsNone(totals["cost"])  # no prices configured

    async def test_the_narrator_speaks_of_her_own_part_in_the_first_person(self):
        club = await self.club(DemoAdapter(), roles={"strong": ["gpt", "claude"]})  # GPT leads and steps in
        task = await club.new(SUNDAY)
        said = [text for who, text in club.said(task) if who == "GPT"]
        self.assertEqual(said[0], "DeepSeek drafts it, Gemini checks it, and I'll step in only if needed.")
        self.assertTrue(said[1].startswith("I'm taking over from DeepSeek: the tests fail"), said[1])

    async def test_the_crew_who_came_along_take_the_roles_first(self):
        club = await self.club(DemoAdapter())
        task = await club.new(SUNDAY, crew=["grok"])
        said = club.said(task)
        self.assertEqual(said[0], ("GPT", "Grok drafts it, Gemini checks it, and Claude steps in only if needed."))
        self.assertEqual(next(step for step in club.board.steps(task) if step["id"] == "r1.draft.1")["seat"], "grok")
        # The page knows who can come (here every key is known from the start), and keeps it for the next start.
        self.assertTrue(all(seat["ready"] for seat in club.page._state()["team"]))
        self.assertEqual(club.board.get("ready")["grok"], True)

    async def test_a_picked_team_takes_its_seats_on_its_models(self):
        club = await self.club(DemoAdapter())
        team = club.page._picked({"formation": "plan", "seats": {"planner": "gemini", "drafter": "grok"},
                                  "tiers": {"planner": "cheap", "drafter": "fancy"}, "tester": False, "budget": "0.05"})
        self.assertEqual(team, {"formation": "plan", "seats": {"planner": "gemini", "drafter": "grok"}, "tester": False,
                                "tiers": {"planner": "cheap"}, "budget": 0.05})
        task = f"t-team{time.time_ns() % 100000}"
        club.board.create_task(task, f"classroom:{task}", "sample", "Sunday", request=SUNDAY, team=team,
                               crew=["gemini", "grok"])
        await club.say(task, SUNDAY, extra={"project": "sample", "workflow": "plan"})
        steps = {step["id"]: step for step in club.board.steps(task)}
        self.assertEqual((steps["r1.plan.1"]["seat"], steps["r1.execute.1"]["seat"]), ("gemini", "grok"))
        plan_calls = [call for step in club.board.steps(task) if step["id"].startswith("r1.plan")
                      for call in step["data"].get("calls", ())]
        self.assertTrue(plan_calls and all(call["tier"] == "cheap" for call in plan_calls))  # her own model, as picked
        view = club.page._task_view(club.task(task))
        self.assertEqual(view["lineup"], [{"persona": "gemini", "role": "planner"}, {"persona": "grok", "role": "drafter"}])
        self.assertEqual(view["data"]["team"]["budget"], 0.05)
        self.assertGreater(view["spend"]["grok"]["calls"], 0)
        self.assertIsNone(view["spend"]["grok"]["cost"])  # no prices configured

    async def test_the_team_picker_refuses_a_team_that_does_not_hold(self):
        club = await self.club(DemoAdapter())
        for given in ({"formation": "plan", "seats": {"planner": "gemini"}},  # the builder's seat is empty
                      {"formation": "alone", "seats": {"drafter": "nobody"}},
                      {"formation": "relay", "seats": {"drafter": "grok"}},
                      {"formation": "alone", "seats": {"drafter": "grok"}, "tester": True},  # a tester, but no seat for her
                      "alone"):
            self.assertIs(club.page._picked(given), False, given)
        tested = club.page._picked({"formation": "alone", "seats": {"drafter": "grok", "tester": "gemini"}, "tester": True,
                                    "budget": 1e9})
        self.assertEqual((tested["seats"], tested["budget"]), ({"drafter": "grok", "tester": "gemini"}, 100.0))

    async def test_the_projects_own_agent_instructions_reach_the_drafter_and_the_judge(self):
        class Recording(DemoAdapter):
            seen: list = []

            async def complete(self, model, messages, schema, schema_name, *args, **kwargs):
                self.seen.append((schema_name, "\n".join(str(message["content"]) for message in messages)))
                return await super().complete(model, messages, schema, schema_name, *args, **kwargs)
        club = await self.club(Recording())
        club.parts["registry"].all()  # the practice project appears on first use
        for name in ("AGENTS.md", "CLAUDE.md"):  # the same words twice (a symlink, say): given once
            (club.sample / name).write_text("Keep every weekday table in DAYS order.\n")
        await club.new(SUNDAY, workflow="critique")
        turn = next(text for name, text in Recording.seen if name == "chimera_turn")
        review = next(text for name, text in Recording.seen if name == "chimera_review")
        for prompt in (turn, review):
            self.assertIn("### AGENTS.md\nKeep every weekday table in DAYS order.", prompt)
        self.assertNotIn("### CLAUDE.md", turn)

    async def test_each_answer_carries_its_price_and_the_task_its_cost(self):
        club = await self.club(DemoAdapter(), prices={"deepseek": {"input": 1.0, "output": 2.0},
                                                      "claude": {"input": 3.0, "output": 15.0},
                                                      "gemini": {"input": 0.5, "output": 1.0},
                                                      "openai": {"input": 1.0, "output": 4.0},
                                                      "grok": {"input": 0.2, "output": 0.5}})
        task = await club.new(SUNDAY)
        calls = [call for step in club.board.steps(task) for call in step["data"].get("calls", ())]
        self.assertTrue(calls and all(call["price"]["source"] == "config" for call in calls))
        totals = club.board.totals(task)
        self.assertAlmostEqual(totals["cost"], sum(call["cost"] for call in calls))
        self.assertGreater(totals["cost"], 0)

    async def test_a_question_is_answered_read_only_without_a_worktree(self):
        club = await self.club(DemoAdapter())
        task = await club.new("What does next_meeting do when the schedule is empty?")
        state = club.task(task)
        self.assertEqual((state["status"], state["workflow"]), ("answered", "single"))
        self.assertNotIn("worktree", state["data"])
        self.assertIn("ValueError", club.said(task)[-1][1])
        self.assertEqual(club.approvals(task), [])

    async def test_a_critique_revises_once_after_a_blocker_from_another_family(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "Wrong greeting.", "summary": "It says bye.", "issues": [
                    {"severity": "blocker", "file": "clubnotes/greet.py", "line": 2, "problem": "says bye",
                     "suggestion": "say hello"}]}
            if "Revise it once" in system:
                return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"}),
                            note="fixing") if turns == 0 else turn(status="done", summary="Now says hello.")
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'bye'\n"})) \
                if turns == 0 else turn(status="done", summary="Added greet.")
        adapter = Scripted(answer)
        club = await self.club(adapter, **TEAM)
        task = await club.new("Add a greet() that says hello.", workflow="critique")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        review = next(step for step in club.board.steps(task) if step["kind"] == "review")
        self.assertEqual((review["seat"], review["data"]["verdict"]), ("checky", "revise"))  # the blocker decides
        self.assertTrue(any(step["id"].startswith("r1.revise.") and step["seat"] == "drafty"
                            for step in club.board.steps(task)))
        worktree = Path(state["data"]["worktree"])
        self.assertIn("hello", (worktree / "clubnotes/greet.py").read_text())
        reviews = [call for call in adapter.calls if call[0] == "review"]
        self.assertEqual(reviews, [("review", "b-large", "medium")])  # strong, another family (review_effort)

    async def test_a_plan_by_a_strong_classmate_is_carried_out_by_another(self):
        briefs = []

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if "You plan it" in system:  # the planner: reads, then answers with the plan
                return turn(("read_file", {"path": "clubnotes/schedule.py"}), note="reading") if turns == 0 else \
                    turn(status="done", summary="1. Add `greet() -> str` in clubnotes/greet.py.\n2. It returns "
                                                "exactly 'hello'.")
            if "Carry it out" in system:
                briefs.append(messages[1]["content"])
                return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"})) \
                    if turns == 0 else turn(status="done", summary="Added greet() as planned.")
            raise AssertionError(f"unexpected call: {kind}")
        adapter = Scripted(answer)
        club = await self.club(adapter, **{**TEAM, "roles": {**TEAM["roles"], "planner": ["checky"]}})
        task = await club.new("Add a greet() that says hello.", workflow="plan")
        state = club.task(task)
        self.assertEqual((state["status"], state["workflow"]), ("review", "plan"))
        steps = club.board.steps(task)
        planning = [step for step in steps if step["kind"] == "turn" and step["id"].startswith("r1.plan.")]
        working = [step for step in steps if step["kind"] == "turn" and step["id"].startswith("r1.execute.")]
        self.assertEqual({step["seat"] for step in planning}, {"checky"})
        self.assertEqual({step["seat"] for step in working}, {"drafty"})
        self.assertIn("The plan from Checky:", briefs[0])
        self.assertIn("returns exactly 'hello'", briefs[0])
        turns = [call for call in adapter.calls if call[0] == "turn"]
        self.assertEqual(turns[0][1], "b-large")  # the planner works on her strong models
        self.assertEqual(turns[-1][1], "a-small")  # the drafter on her cheap ones
        self.assertTrue(any("Checky plans it, then Drafty carries out the plan." in text for _, text in club.said(task)))
        worktree = Path(state["data"]["worktree"])
        self.assertIn("hello", (worktree / "clubnotes/greet.py").read_text())

    async def test_a_planner_who_cannot_plan_leaves_the_drafter_the_task(self):
        briefs = []

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if "You plan it" in system:
                return turn(status="blocked", summary="I cannot read this project.")
            briefs.append(messages[1]["content"])
            return turn(("write_file", {"path": "NEW.md", "content": "x\n"})) if turns == 0 else \
                turn(status="done", summary="NEW.md")
        club = await self.club(Scripted(answer), **{**TEAM, "roles": {**TEAM["roles"], "planner": ["checky"]}})
        task = await club.new("Add NEW.md.", workflow="plan")
        self.assertEqual(club.task(task)["status"], "review")
        self.assertNotIn("The plan from", briefs[0])

    async def test_a_change_nobody_can_review_comes_to_you_marked_unreviewed(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return "this is not JSON"
            return turn(("write_file", {"path": "NEW.md", "content": "x\n"})) if turns == 0 else turn(status="done", summary="NEW.md")
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Add NEW.md.", workflow="critique")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertIn("nobody could review", state["data"]["warning"])
        checklist = json.loads(club.approvals(task, "chimera.review")[-1]["data"]["arguments"])["checklist"]
        self.assertIn("unreviewed", [item["id"] for item in checklist])

    async def test_a_change_whose_tests_fail_applies_nothing(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if turns == 0:
                return turn(("edit_file", {"path": "clubnotes/schedule.py", "old": "return meetings",
                                           "new": "return []"}))
            return turn(status="done", summary="Done.")
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Break the parser.", workflow="single")
        state = club.task(task)
        self.assertEqual(state["status"], "failed")
        self.assertIn("tests fail", state["data"]["failure"])
        self.assertEqual(club.approvals(task), [])
        await club.say(task, f"$land {task}")
        self.assertEqual(club.approvals(task)[-1]["data"]["kind"], "confirm")  # landing always asks to confirm
        await club.decide(task, "chimera.land")
        self.assertEqual(club.task(task)["status"], "failed")
        self.assertEqual(git(club.sample, "rev-list", "--count", "HEAD"), "1")
        self.assertTrue(any("did not pass" in text for _, text in club.said(task)[-2:]))  # the refusal, then the receipt

    async def test_a_new_project_gets_its_tests_run_once_the_club_writes_them(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            if turns == 0:
                return turn(("write_file", {"path": "adder.py", "content": "def add(a, b):\n    return a + b\n"}),
                            ("write_file", {"path": "tests/__init__.py", "content": ""}),
                            ("write_file", {"path": "tests/test_adder.py", "content":
                                            "import unittest\nfrom adder import add\n\nclass T(unittest.TestCase):\n"
                                            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"}))
            return turn(status="done", summary="Added add() with a test.")
        club = await self.club(Scripted(answer), **TEAM)
        club.parts["registry"].create("fresh")
        task = await club.new("Write an add function with a test.", project="fresh")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertTrue(state["data"]["validation"]["passed"])
        self.assertIn("unittest", state["data"]["validation"]["command"])

    async def test_a_classmate_who_gets_stuck_mid_task_is_relieved_without_redoing_her_turns(self):
        class Flaky(Scripted):
            async def complete(self, model, messages, schema, schema_name, *args, **kwargs):
                turns = sum("Results of your actions" in message["content"] for message in messages)
                if model == "a-small" and schema_name == "chimera_turn" and turns >= 1:
                    raise TimeoutError("too slow")
                return await super().complete(model, messages, schema, schema_name, *args, **kwargs)

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            if turns == 0:
                return turn(("read_file", {"path": "README.md"}), say="Reading first.")
            if turns == 1:
                return turn(("write_file", {"path": "DONE.md", "content": "done\n"}))
            return turn(status="done", summary="Wrote DONE.md.")
        club = await self.club(Flaky(answer), **TEAM)
        task = await club.new("Write DONE.md.", workflow="single")
        steps = {step["id"]: step for step in club.board.steps(task)}
        self.assertEqual(steps["r1.single.1"]["seat"], "drafty")       # her turn stays hers
        self.assertEqual(steps["r1.single.2"]["seat"], "checky")       # the rest is Checky's
        from extensions.chimera import views                          # both are on the session's strip
        self.assertEqual(set(views.task_view(club.board, club.task(task), "classroom:")["people"]), {"drafty", "checky"})
        self.assertEqual(len(steps["r1.single.1"]["data"]["actions"]), 1)
        self.assertEqual(len(steps["r1.single.1"]["data"]["calls"]), 1)
        self.assertTrue(any("got stuck" in text for _, text in club.said(task)))
        self.assertEqual(club.task(task)["status"], "review")

    async def test_an_answer_cut_off_at_the_length_limit_is_asked_again_with_twice_the_room(self):
        class Long(Scripted):
            budgets: list = []

            async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
                if schema_name == "chimera_turn":
                    self.budgets.append((max_tokens, effort))
                    if len(self.budgets) == 1:  # her first answer wrote too much at once
                        return AdapterResult('{"say": "Writing it all', 120, max_tokens, truncated=True)
                return await super().complete(model, messages, schema, schema_name, effort, max_tokens, timeout_s)

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "turn":
                self.assertIn("write a big file in parts", messages[-1]["content"]) if turns == 0 else None
                return turn(("write_file", {"path": "DONE.md", "content": "done\n"})) if turns == 0 else \
                    turn(status="done", summary="Wrote DONE.md.")
            return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
        club = await self.club(Long(answer), **TEAM)
        task = await club.new("Write DONE.md.", workflow="single")
        self.assertEqual(Long.budgets[:2], [(8000, "medium"), (16000, "low")])  # more room, less hidden reasoning
        self.assertEqual(club.task(task)["status"], "review")
        self.assertFalse(any("stuck" in text for _, text in club.said(task)))  # still her task

    async def test_seats_without_a_key_are_skipped_quietly_and_a_sibling_role_steps_in(self):
        team = {**TEAM, "team": {**TEAM["team"], "nokey": {"name": "Nokey", "family": "gamma", "cheap": ["gamma/g"]}},
                "roles": {**TEAM["roles"], "strong": ["nokey", "checky"]},
                "providers": {**TEAM["providers"], "gamma": {"kind": "openai_compatible", "api_key_env": "NO_SUCH_KEY"}}}

        class Stuck(Scripted):
            async def complete(self, model, messages, schema, schema_name, *args, **kwargs):
                if model == "b-large" and schema_name == "chimera_turn":
                    return AdapterResult("not JSON at all", 100, 10)  # Checky, the strong one, can't be read
                return await super().complete(model, messages, schema, schema_name, *args, **kwargs)

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="large")
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            return turn(("write_file", {"path": "BIG.md", "content": "big\n"})) if turns == 0 else \
                turn(status="done", summary="Wrote BIG.md.")
        adapter = Stuck(answer)
        club = await self.club(adapter, **team)
        club.core.providers._given.discard("gamma")  # Nokey has no key here
        club.core.providers._adapters.pop("gamma", None)
        task = await club.new("Write BIG.md.", workflow="critique")  # large: drafted in the strong role
        said = " ".join(text for _, text in club.said(task))
        self.assertNotIn("Nokey", said)                      # never tried, never announced
        self.assertIn("Checky got stuck", said)
        self.assertEqual(club.task(task)["status"], "review")  # Drafty, a drafter, took the strong part over

    async def test_a_command_off_the_allowlist_waits_for_approval_and_the_run_resumes(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            if turns == 0:
                return turn(("run", {"command": "touch approved.txt"}))
            return turn(status="done", summary="Made the file.")
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Make an empty approved.txt.")
        self.assertEqual(club.task(task)["phase"], "waiting for you")
        card = club.approvals(task, "chimera.run")[-1]["data"]
        self.assertEqual(json.loads(card["arguments"])["command"], "touch approved.txt")
        await club.decide(task, "chimera.run")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertEqual([item["path"] for item in state["data"]["change"]["files"]], ["approved.txt"])
        actions = next(step for step in club.board.steps(task) if step["id"] == "r1.draft.1")["data"]["actions"]
        self.assertEqual(actions[0]["status"], "success")

    async def test_a_denied_second_check_keeps_the_change_and_lands_nothing(self):
        club = await self.club(DemoAdapter())
        task = await club.new(SUNDAY)
        await club.decide(task, "chimera.review", approve=False)
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertTrue(Path(state["data"]["worktree"]).is_dir())
        self.assertIn("nothing was applied", club.said(task)[-1][1])
        self.assertEqual(club.approvals(task, "chimera.land"), [])
        await club.say(task, f"$discard {task}")
        self.assertEqual(club.task(task)["status"], "discarded")
        self.assertFalse(Path(state["data"]["worktree"]).exists())

    async def test_a_follow_up_is_another_round_on_the_same_change_set(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            followup = "build on it" in system.lower()
            path = "SECOND.md" if followup else "FIRST.md"
            return turn(("write_file", {"path": path, "content": "x\n"})) if turns == 0 else turn(status="done", summary=path)
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Add FIRST.md.")
        first = club.task(task)["data"]["change"]["fingerprint"]
        await club.say(task, "Also add SECOND.md.")
        state = club.task(task)
        self.assertEqual(state["data"]["rounds"], 2)
        self.assertEqual(sorted(item["path"] for item in state["data"]["change"]["files"]), ["FIRST.md", "SECOND.md"])
        self.assertNotEqual(state["data"]["change"]["fingerprint"], first)
        cards = club.approvals(task, "chimera.review")
        self.assertEqual(len(cards), 2)  # the first round's card is now for an older change set
        await club.decide(task, "chimera.review")
        await club.decide(task, "chimera.land")
        self.assertEqual(club.task(task)["status"], "landed")

    async def test_feedback_while_the_club_works_is_taken_in_the_same_run(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class Slow(Scripted):
            async def complete(self, *args, **kwargs):
                if kwargs.get("schema_name", args[3] if len(args) > 3 else "") == "chimera_turn" and not started.is_set():
                    started.set()
                    await release.wait()
                return await super().complete(*args, **kwargs)

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            text = "\n".join(message["content"] for message in messages)
            path = "EXTRA.md" if "New request from" in text else "MAIN.md"
            return turn(("write_file", {"path": path, "content": "x\n"})) if turns == 0 else turn(status="done", summary=path)
        club = await self.club(Slow(answer), **TEAM)
        task = f"t-slow{time.time_ns() % 100000}"
        club.board.create_task(task, f"classroom:{task}", "sample", "Main", request="Add MAIN.md.")
        await club.submit(Request(id="first", address=Address("classroom", task), sender=Sender("creator", "Leo"),
                                            text="Add MAIN.md.", received_at=time.time(), display=club.page.display,
                                            extra={"task": task, "project": "sample"}))
        await asyncio.wait_for(started.wait(), 10)
        await club.submit(Request(id="second", address=Address("classroom", task), sender=Sender("creator", "Leo"),
                                            text="Also add EXTRA.md.", received_at=time.time(), display=club.page.display,
                                            extra={"task": task}))
        release.set()
        await asyncio.sleep(0.1)
        await club.idle()
        state = club.task(task)
        self.assertEqual(sorted(item["path"] for item in state["data"]["change"]["files"]), ["EXTRA.md", "MAIN.md"])
        self.assertEqual(state["data"]["rounds"], 2)
        self.assertEqual(state["status"], "review")

    async def test_a_task_waiting_for_approval_survives_a_restart(self):
        club = await self.club(DemoAdapter())
        task = await club.new(SUNDAY)
        await club.stop()
        self.clubs.remove(club)
        again = await self.club(DemoAdapter())
        self.assertEqual(again.task(task)["status"], "review")
        await again.decide(task, "chimera.review")
        await again.decide(task, "chimera.land")
        self.assertEqual(again.task(task)["status"], "landed")
        said = [text for _, text in again.said(task)]
        self.assertEqual(sum(text.startswith("Ready for your review") for text in said), 1)  # nothing said twice

    async def test_someone_else_gets_a_friendly_answer_and_no_projects(self):
        club = await self.club(DemoAdapter())
        task = f"t-guest{time.time_ns() % 100000}"
        club.board.create_task(task, f"classroom:{task}", "sample", "Hi", request="hi")
        await club.say(task, "Can you fix my code?", sender=("someone", "Guest"))
        self.assertEqual(club.task(task)["status"], "queued")  # never started as a task
        self.assertIn("demo club", club.said(task)[-1][1])


class PageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.club = await Club(Path(self._tmp.name), DemoAdapter()).start()
        self.url = f"http://127.0.0.1:{self.club.page.port_number}"

    async def asyncTearDown(self):
        await self.club.stop()
        self._tmp.cleanup()

    async def test_other_sites_are_refused_and_the_page_starts_tasks(self):
        async with aiohttp.ClientSession() as http:
            response = await http.get(self.url + "/", headers={"Origin": "http://evil.example"})
            self.assertEqual(response.status, 403)
            response = await http.get(self.url + "/")
            self.assertIn("Content-Security-Policy", response.headers)
            async with http.ws_connect(self.url + "/ws", headers={"Origin": self.url}) as ws:
                hello = await ws.receive_json()
                self.assertEqual(hello["type"], "hello")
                self.assertEqual([project["name"] for project in hello["state"]["projects"]], ["sample"])
                await ws.send_json({"type": "start", "id": "c1", "project": "sample", "workflow": "auto", "text": SUNDAY})
                ack = None
                while ack is None:
                    message = await asyncio.wait_for(ws.receive_json(), 10)
                    ack = message if message["type"] == "ack" else None
                self.assertTrue(ack["ok"])
            await self.club.idle()
            task = ack["task"]
            self.assertEqual(self.club.task(task)["status"], "review")
            diff = await (await http.get(f"{self.url}/api/tasks/{task}/diff")).json()
            self.assertTrue(diff["live"])
            self.assertIn("clubnotes/schedule.py", diff["patch"])
            data = await (await http.get(f"{self.url}/api/tasks/{task}")).json()
            self.assertEqual(data["messages"][0]["kind"], "mine")
            self.assertEqual((await http.get(f"{self.url}/api/tasks/{task}/file?path=../../x")).status, 403)


# ---- inside the real core --------------------------------------------------------------------------------------
class CoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_module_runs_a_task_through_the_core(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            team = {"team": {"gpt": False, "claude": False, "gemini": False, "grok": False, "deepseek": False,
                             "drafty": {"name": "Drafty", "family": "alpha", "cheap": ["fake/drafty"]},
                             "checky": {"name": "Checky", "family": "beta", "cheap": ["fake/checky"]}},
                    "roles": {"router": ["checky"], "drafter": ["drafty"], "judge": ["checky"], "strong": ["checky"],
                              "critic": ["checky"]},
                    "port": 0, "host": "127.0.0.1"}
            core = await start_core(tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                                                         str(ROOT / "extensions" / "chimera")],
                                          "extension_config": {"chimera": team},
                                          "assignments": {"*": {"engine": "probe", "personas": ["p"]}}},
                                    adapter=DemoAdapter())
            try:
                page = iface(core, "classroom")
                board = page.board
                task = "t-core0001"
                board.create_task(task, f"classroom:{task}", "sample", "Sunday", request=SUNDAY)

                async def say(text, ident):
                    await page.port.submit(Request(id=ident, address=Address("classroom", task),
                                                   sender=Sender("creator", "Leo"), text=text, received_at=time.time(),
                                                   display=page.display, extra={"task": task, "project": "sample"}))
                    for _ in range(40):
                        await settle(core)
                        await asyncio.sleep(0.05)
                await say(SUNDAY, "c1")
                self.assertEqual(board.task(task)["status"], "review")
                cards = [item for item in board.messages(task) if item.get("code") == "approval_request"]
                self.assertEqual(cards[-1]["data"]["capability"], "chimera.review")
                await say(f"$approve {cards[-1]['data']['approval_id']}", "c2")  # nothing ticked: not yet
                again = [item for item in board.messages(task) if item.get("code") == "approval_request"]
                self.assertEqual((again[-1]["data"]["capability"], board.task(task)["status"]), ("chimera.review", "review"))
                self.assertNotEqual(again[-1]["data"]["approval_id"], cards[-1]["data"]["approval_id"])  # asked anew
                self.assertTrue(any(item.get("text", "").startswith("Not yet:") for item in board.messages(task)))
                await say("$check all", "c2b")                                 # looked at everything
                await say(f"$approve {again[-1]['data']['approval_id']}", "c2c")
                cards = [item for item in board.messages(task) if item.get("code") == "approval_request"]
                self.assertEqual(cards[-1]["data"]["capability"], "chimera.land")
                await say(f"$approve {cards[-1]['data']['approval_id']}", "c3")
                self.assertEqual(board.task(task)["status"], "landed")
                sample = Path(core.config.data_dir) / "extensions" / "chimera" / "sample"
                self.assertEqual(git(sample, "rev-list", "--count", "HEAD"), "2")
            finally:
                await core.stop()

    async def test_classmates_without_a_key_are_passed_over_quietly_in_the_core(self):
        """The core says which profiles have a key (Models.ready, contracts 1.6), so nobody is announced who can't come."""
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            team = {"team": {"gpt": False, "claude": False, "gemini": False, "grok": False, "deepseek": False,
                             "nokey": {"name": "Nokey", "family": "gamma", "cheap": ["keyless/k-1"]},
                             "drafty": {"name": "Drafty", "family": "alpha", "cheap": ["fake/drafty"]},
                             "checky": {"name": "Checky", "family": "beta", "cheap": ["fake/checky"]}},
                    "roles": {"router": ["nokey", "checky"], "drafter": ["nokey", "drafty"], "judge": ["checky"],
                              "strong": ["checky"], "critic": ["checky"]},
                    "providers": {"keyless": {"kind": "openai_compatible", "api_key_env": "WB_TEST_NO_SUCH_KEY"}},
                    "port": 0, "host": "127.0.0.1"}
            core = await start_core(tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                                                         str(ROOT / "extensions" / "chimera")],
                                          "extension_config": {"chimera": team}}, adapter=DemoAdapter())
            try:
                self.assertFalse(core.manager.providers.profile_ready("chimera_nokey_cheap"))
                self.assertTrue(core.manager.providers.profile_ready("chimera_drafty_cheap"))
                page = iface(core, "classroom")
                task = "t-quiet001"
                page.board.create_task(task, f"classroom:{task}", "sample", "Sunday", request=SUNDAY)
                await page.port.submit(Request(id="q1", address=Address("classroom", task), sender=Sender("creator", "Leo"),
                                               text=SUNDAY, received_at=time.time(), display=page.display,
                                               extra={"task": task, "project": "sample"}))
                for _ in range(40):
                    await settle(core)
                    await asyncio.sleep(0.05)
                said = [item["text"] for item in page.board.messages(task) if item.get("kind") == "text"]
                self.assertTrue(said[0].startswith("Drafty drafts it"), said[0])
                self.assertFalse(any("can't make it" in text for text in said))
            finally:
                await core.stop()

    async def test_a_task_from_another_interface_gets_the_diff_and_a_link_and_lands_from_there(self):
        """As from Discord: the conversation's assignment names the project; the creator approves where they asked."""
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            team = {"team": {"gpt": False, "claude": False, "gemini": False, "grok": False, "deepseek": False,
                             "drafty": {"name": "Drafty", "family": "alpha", "cheap": ["fake/drafty"]},
                             "checky": {"name": "Checky", "family": "beta", "cheap": ["fake/checky"]}},
                    "roles": {"router": ["checky"], "drafter": ["drafty"], "judge": ["checky"], "strong": ["checky"],
                              "critic": ["checky"]},
                    "port": 0, "host": "127.0.0.1"}
            core = await start_core(tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                                                         str(ROOT / "extensions" / "chimera")],
                                          "extension_config": {"chimera": team},
                                          "assignments": {"*": {"engine": "probe", "personas": ["p"]},
                                                          "chat:room": {"engine": "chimera",
                                                                        "personas": ["drafty", "checky"],
                                                                        "options": {"project": "sample"}}}},
                                    adapter=DemoAdapter())
            try:
                chat = iface(core, "chat")

                async def wait():
                    for _ in range(40):
                        await settle(core)
                        await asyncio.sleep(0.05)
                await chat.say(SUNDAY, sender="boss", name="Boss")
                await wait()
                board = iface(core, "classroom").board
                task = board.latest("chat:room")
                self.assertEqual((task["project"], task["status"]), ("sample", "review"))
                texts = chat.texts("room")
                self.assertTrue(any(f"/#{task['id']}/changes" in text for text in texts), texts)
                files = [reply for reply in chat.delivered if reply.kind == "file"]
                self.assertEqual(files[-1].file.name, f"{task['id']}.diff")
                await chat.say(f"$check {task['id']} all", sender="boss", name="Boss")  # the second check, from here
                await wait()
                self.assertIn("Everything is checked", chat.texts("room")[-1])
                for capability in ("chimera.review", "chimera.land"):
                    card = chat.notices("approval_request")[-1]
                    self.assertEqual((card.address.conversation, card.data["capability"]), ("room", capability))
                    await chat.say(f"$approve {card.data['approval_id']}", sender="boss", name="Boss")
                    await wait()
                self.assertEqual(board.task(task["id"])["status"], "landed")
                self.assertTrue(any(text.startswith("Landed as") for text in chat.texts("room")))
            finally:
                await core.stop()

    async def test_a_trip_from_the_classroom_to_the_lab_and_back(self):
        """A classmate invites you; the Classroom starts from what you talked about; the result goes back to the chat."""
        class Recording(DemoAdapter):
            def __init__(self):
                self.seen = []

            async def complete(self, model, messages, schema, schema_name, *args, **kwargs):
                self.seen.append((schema_name, messages))
                return await super().complete(model, messages, schema, schema_name, *args, **kwargs)
        adapter = Recording()
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            team = {"team": {"gpt": False, "claude": False, "gemini": False, "grok": False, "deepseek": False,
                             "drafty": {"name": "Drafty", "family": "alpha", "cheap": ["fake/drafty"], "also": ["pat"]},
                             "checky": {"name": "Checky", "family": "beta", "cheap": ["fake/checky"]}},
                    "roles": {"router": ["checky"], "drafter": ["drafty"], "judge": ["checky"], "strong": ["checky"],
                              "critic": ["checky"]},
                    "port": 0, "host": "127.0.0.1"}
            core = await start_core(tmp, {"extensions": [str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                                                         str(ROOT / "extensions" / "chimera")],
                                          "extension_config": {"chimera": team}}, adapter=adapter)
            try:
                chat, page = iface(core, "chat"), iface(core, "classroom")
                board = page.board

                async def wait():
                    for _ in range(40):
                        await settle(core)
                        await asyncio.sleep(0.05)
                await chat.say("The schedule lists Sunday meetings first; they should come last.", sender="boss",
                               name="Boss")
                await wait()
                invite = {"brief": SUNDAY, "classmate": "Pat", "notes": "Boss likes a regression test with every fix.",
                          "title": "Sunday last", "companions": ["Checky", "Nobody"]}
                await chat.say(f"$call chimera.invite {json.dumps(invite)}", sender="boss", name="Boss")
                await wait()
                task = board.latest("classroom:" + board.tasks()[0]["id"])
                self.assertEqual((task["status"], task["project"]), ("invited", "sample"))
                invited = task["data"]["invited"]
                self.assertEqual((invited["lead"], invited["from"]), ("drafty", "chat:room"))  # Pat stands for Drafty
                self.assertEqual(invited["crew"], ["checky"])  # Nobody is not in the lab's team
                self.assertIn("Sunday meetings first", " ".join(item["text"] for item in invited["excerpt"]))
                self.assertTrue(any(f"/#{task['id']}" in text for text in chat.texts("room")))  # the link to follow
                presence = page.presence()
                self.assertEqual(presence["tickets"][0]["task"], task["id"])   # a ticket in the corridor
                self.assertNotIn("pat", presence["away"])                      # while Pat waits in class
                self.assertEqual(presence["people"], [])
                page._publish()                                                # what other interfaces see
                seen = iface(core, "chat").port.directory()["classroom"]
                self.assertEqual((seen["link"], seen["status"]["place"]["name"]), (page.url, "Classroom"))
                await page.port.submit(Request(id="accept", address=Address("classroom", task["id"]),
                                               sender=Sender("creator", "Leo"), text=SUNDAY, received_at=time.time(),
                                               display=page.display, extra={"task": task["id"], "project": "sample"}))
                await wait()
                task = board.task(task["id"])
                self.assertEqual(task["status"], "review")
                spoken = [item["text"] for item in board.messages(task["id"]) if item.get("speaker") == "Drafty"]
                self.assertTrue(spoken[0].startswith("Here we are in the Classroom, with Checky."), spoken[0])
                presence = page.presence()                                     # the visit: both in the Classroom
                self.assertEqual({person["key"] for person in presence["people"]}, {"drafty", "checky"})
                self.assertIn("pat", presence["away"])                         # Pat's MomoChat self came along
                self.assertEqual(presence["people"][0]["doing"], "waiting for your check")
                brief = next(messages for name, messages in adapter.seen if name == "chimera_turn")[1]["content"]
                self.assertIn("The conversation in MomoChat", brief["content"] if isinstance(brief, dict) else brief)
                self.assertIn("regression test with every fix", brief)
                await page.port.submit(Request(id="check-all", address=Address("classroom", task["id"]),
                                               sender=Sender("creator", "Leo"), text="$check all", received_at=time.time(),
                                               display=page.display, extra={"task": task["id"]}))
                await wait()
                for capability in ("chimera.review", "chimera.land"):
                    card = [item for item in board.messages(task["id"]) if item.get("code") == "approval_request"][-1]
                    self.assertEqual(card["data"]["capability"], capability)
                    await page.port.submit(Request(id=f"ok-{capability}", address=Address("classroom", task["id"]),
                                                   sender=Sender("creator", "Leo"),
                                                   text=f"$approve {card['data']['approval_id']}",
                                                   received_at=time.time(), display=page.display,
                                                   extra={"task": task["id"]}))
                    await wait()
                self.assertEqual(board.task(task["id"])["status"], "landed")
                await page._report(None, task["id"])
                await wait()
                report = chat.texts("room")[-1] if "Back from the Classroom" in chat.texts("room")[-1] else \
                    next(text for text in chat.texts("room") if "Back from the Classroom" in text)
                self.assertIn("landed as", report)
                self.assertIn("In the Classroom: Checky, Drafty.", report)  # who was really there
                recorded = [item.text for item in core.manager.tank.recent_messages("chat:room", 50)]
                self.assertTrue(any("Back from the Classroom" in text for text in recorded))       # MomoChat knows it
                self.assertEqual(board.task(task["id"])["data"]["reported"]["to"], "chat:room")
                self.assertEqual(page.presence()["away"], [])                  # back in class with the result
                await chat.say("$tasks sunday", sender="boss", name="Boss")                 # the Logbook
                await wait()
                self.assertIn(task["id"], chat.texts("room")[-1])
            finally:
                await core.stop()


# ---- verification: criteria, reviews that follow them, the tester and one repair ---------------------------------
GREET = "Add greet() in clubnotes/greet.py that returns 'hello'. Keep the list output unchanged."
VERIFY = {**TEAM, "verify": {"requirements": True, "probe": True, "repairs": 1}}


def criteria(_messages=None):
    return {"requirements": [{"text": "greet() returns 'hello'", "kind": "behavior"},
                             {"text": "The list output stays the same", "kind": "keep"}]}


class VerifyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.clubs = []

    async def asyncTearDown(self):
        for club in self.clubs:
            await club.stop()
        self._tmp.cleanup()

    async def club(self, adapter, **config):
        club = await Club(self.tmp, adapter, **config).start()
        self.clubs.append(club)
        return club

    async def test_criteria_reach_the_drafter_and_a_minor_bug_or_an_unasked_change_still_blocks(self):
        seen = {}

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="medium")
            if kind == "requirements":
                return criteria()
            if kind == "review":
                seen.setdefault("review", messages[-1]["content"])
                if len(seen.setdefault("reviews", [])) == 0:
                    seen["reviews"].append(1)
                    return {"verdict": "approve", "say": "", "summary": "Fine, one nit.",
                            "requirements": [{"id": "R1", "met": "yes", "evidence": "returns 'hello'"},
                                             {"id": "R2", "met": "yes", "evidence": "untouched"}],
                            "unexplained": [{"file": "README.md", "change": "rewrote the intro"}],
                            "issues": [{"severity": "minor", "category": "correctness", "file": "clubnotes/greet.py",
                                        "line": 2, "problem": "returns 'Hello' with a capital", "suggestion": "lowercase"}]}
                return {"verdict": "approve", "say": "", "summary": "Good.", "issues": []}
            if kind == "turn" and "the club's tester" in system:
                return turn(status="done", summary="Probed it.")
            seen.setdefault("brief", messages[1]["content"])
            done = "Revise it once" in system
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": f"def greet():\n    return "
                         f"{chr(39)}{'hello' if done else 'Hello'}{chr(39)}\n"})) if turns == 0 else \
                turn(status="done", summary="greet() added.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="critique")
        self.assertIn("Acceptance criteria", seen["brief"])
        self.assertIn("R2 (keep): The list output stays the same", seen["brief"])
        self.assertIn("R1 (behavior): greet() returns 'hello'", seen["review"])     # the reviewer gets them too
        review = next(step for step in club.board.steps(task) if step["id"] == "r1.review")
        self.assertEqual(review["data"]["verdict"], "revise")  # "approve" with a minor bug and an unasked change
        self.assertTrue(any(step["id"].startswith("r1.revise.") for step in club.board.steps(task)))
        self.assertEqual([item["id"] for item in club.task(task)["data"]["requirements"]], ["R1", "R2"])

    async def test_a_follow_up_adds_only_the_criteria_it_brings(self):
        asked = []

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            if kind == "requirements":
                asked.append(messages[-1]["content"])
                if len(asked) == 1:
                    return criteria()
                return {"requirements": [{"text": f"extra {n}", "kind": "other"} for n in range(8)]}
            if "the club's tester" in system:
                return turn(status="done", summary="Probed.")
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"})) \
                if turns == 0 else turn(status="done", summary="Done.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="single")
        await club.say(task, "Also make it say hello twice.")
        self.assertIn("List only criteria it adds or changes", asked[1])
        self.assertIn("- greet() returns 'hello'", asked[1])          # what is there already
        self.assertEqual(len(club.task(task)["data"]["requirements"]), 2 + 5)  # a follow-up adds a few at most

    async def test_the_tester_finds_a_problem_it_is_repaired_and_tested_again(self):
        probes = []

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            if kind == "requirements":
                return criteria()
            if kind == "review":
                return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
            text = "\n".join(str(message["content"]) for message in messages)
            if "the club's tester" in system:
                probes.append(turns)
                if turns == 0:
                    return turn(("probe_file", {"path": "greet.py", "content": "import sys\nsys.path.insert(0, '.')\n"
                                                "from clubnotes.greet import greet\nprint(greet())\n"}),
                                ("probe", {"command": "python3 .probe/greet.py"}))
                fixed = "hello" in text.split("## New code", 1)[-1].split("## Old code", 1)[0]
                found = [] if fixed else [{"criterion": "R1", "problem": "greet() says hi", "evidence": "printed hi"}]
                return {**turn(status="done", summary="Probed."), "findings": found}
            if "tester tried to break" in system:  # the repair
                return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"})) \
                    if turns == 0 else turn(status="done", summary="Now hello.")
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hi'\n"})) \
                if turns == 0 else turn(status="done", summary="greet() added.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="single")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        verification = state["data"]["verification"]
        self.assertEqual((verification["repairs"], verification["findings"]), (1, []))  # found, fixed, clean
        self.assertEqual(len(verification["rounds"]), 2)
        self.assertTrue(any(step["id"].startswith("r1.repair1.") for step in club.board.steps(task)))
        worktree = Path(state["data"]["worktree"])
        self.assertIn("hello", (worktree / "clubnotes/greet.py").read_text())
        self.assertNotIn(".probe/x.txt", [item["path"] for item in state["data"]["change"]["files"]])  # never landed
        said = " ".join(text for _, text in club.said(task))
        self.assertIn("found 1 problem", said)
        self.assertEqual(state["data"]["summary"], "greet() added.\n\n**After the tester's findings:** Now hello.")
        checklist = json.loads(club.approvals(task, "chimera.review")[-1]["data"]["arguments"])["checklist"]
        self.assertIn("Tester: 2 probe(s), nothing broke after 1 repair(s)",
                      [item["label"] for item in checklist])

    async def test_what_the_tester_still_finds_comes_to_you_on_the_checklist(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            if kind == "requirements":
                return criteria()
            if "the club's tester" in system:
                return {**turn(status="done", summary="Probed."),
                        "findings": [{"criterion": "R2", "problem": "list output changed", "evidence": "diff of outputs"}]}
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"})) \
                if turns == 0 else turn(status="done", summary="Done.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="single")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertIn("still sees 1 problem", state["data"]["warning"])
        labels = [item["label"] for item in json.loads(
            club.approvals(task, "chimera.review")[-1]["data"]["arguments"])["checklist"]]
        self.assertIn("Still wrong: list output changed", labels)  # you tick it only after looking at it

    async def test_a_probe_runs_the_same_line_on_the_old_code_and_the_new(self):
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            return turn(("edit_file", {"path": "clubnotes/schedule.py", "old": 'return f"{self.day} {self.time} {self.name}"',
                                       "new": 'return f"{self.day} {self.time} · {self.name}"'})) if turns == 0 \
                else turn(status="done", summary="Dotted.")
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Put a dot between time and name.", workflow="single")
        state = club.task(task)
        chimera = club.page.chimera
        context = SimpleNamespace(address=Address("classroom", task))
        await chimera.probe_file({"task": task, "path": "week.txt", "content": "Mon 15:30 Chess\n"}, context)
        result = await chimera.probe({"task": task, "command": "python3 -c \"from clubnotes.schedule import parse; "
                                        "print(parse(open('.probe/week.txt').read())[0])\""}, context)
        self.assertFalse(result.data["same"])
        self.assertIn("Mon 15:30 · Chess", result.summary.split("## Old code")[0])
        self.assertIn("Mon 15:30 Chess", result.summary.split("## Old code")[1])
        change = (await chimera.diff({"task": task}, context)).data
        self.assertEqual([item["path"] for item in change["files"]], ["clubnotes/schedule.py"])  # .probe/ left out
        self.assertTrue((Path(state["data"]["worktree"]).parent / f"{task}.base").is_dir())

    async def test_a_workflow_dropped_into_the_registry_is_routed_to_by_config(self):
        import importlib
        club = await self.club(Scripted(lambda *args: None), **TEAM, routing={"small": "tiny_test"})
        # The core loads the module under a package of its own: register where it looks (a file in workflows/ does).
        workflows = importlib.import_module(type(club.page).__module__.rsplit(".", 2)[0] + ".workflows")

        @workflows.workflow("tiny_test", "Tiny test", "A test workflow")
        async def tiny_test(round):
            await round.say("route.say", "Doing it the tiny_test way.")
            solved = await round.solve(f"{round.r}.single", "drafter", "single", round.base_brief, turns="single")
            return round.outcome(solved, await round.diff("t.diff"), await round.validate("t.test"))
        try:
            def answer(kind, system, messages, turns):
                if kind == "route":
                    return route(size="small")
                return turn(("write_file", {"path": "T.md", "content": "t\n"})) if turns == 0 \
                    else turn(status="done", summary="T.md")
            club.adapter.answer = answer
            task = await club.new("Add T.md.")
            self.assertEqual(club.task(task)["workflow"], "tiny_test")
            self.assertIn("Doing it the tiny_test way.", [text for _, text in club.said(task)])
            self.assertIn("tiny_test", [item["name"] for item in club.page._state()["workflows"]])
        finally:
            workflows.REGISTRY.pop("tiny_test", None)

    async def test_without_the_page_the_api_still_serves_club_and_other_frontends(self):
        club = await self.club(DemoAdapter(), web=False)
        url = club.page.url
        async with aiohttp.ClientSession() as http:
            page = await http.get(url + "/")
            self.assertIn("web page is turned off", await page.text())
            self.assertEqual((await http.get(url + "/static/app.js")).status, 404)
            state = await (await http.get(url + "/api/state")).json()
            self.assertEqual(state["page"], False)
            self.assertEqual(state["places"]["here"]["place"]["name"], "Classroom")
            # its entries in every page's places bar (the web UI's Cafe and MomoChat come before them)
            self.assertEqual([(view["name"], view["path"], view["order"]) for view in state["places"]["here"]["views"]],
                             [("Classroom", "", 30), ("Logbook", "#logbook", 50)])  # (Students is the web UI's)


# ---- `club`, the lab from a terminal --------------------------------------------------------------------------
class CliTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_a_folder_becomes_a_repository_only_when_you_agree_and_a_subfolder_means_its_repository(self):
        from extensions.chimera import cli
        said = []
        folder = self.tmp / "notes"
        folder.mkdir()
        (folder / "a.txt").write_text("hello\n")
        self.assertIsNone(cli.workplace(folder, False, said.append))  # no terminal, no --yes: nothing changes
        self.assertFalse((folder / ".git").exists())
        self.assertIn("not a git repository", said[0])
        self.assertEqual(cli.workplace(folder, True, said.append), folder.resolve())
        self.assertEqual(git(folder, "log", "--format=%s"), "Start notes")  # its files in a first commit
        (folder / "src").mkdir()
        self.assertEqual(cli.workplace(folder / "src", False, said.append), folder.resolve())
        self.assertIsNone(cli.workplace(cli.DATA / "worktrees", False, said.append))  # the lab's own folders
        self.assertIsNone(cli.workplace(self.tmp / "missing", True, said.append))

    async def test_the_lab_says_which_approval_is_for_now_and_club_only_asks(self):
        from extensions.chimera import cli, views
        from extensions.chimera.board import Board
        board = Board(self.tmp / "board.db")
        board.create_task("t-1", "classroom:t-1", "sample", "A task")
        board.update_task("t-1", status="review", data={"change": {"fingerprint": "f1"}})
        board.put_step("t-1", "r1.check", kind="approval", status="running",
                       data={"stage": "review", "fingerprint": "f1", "checklist": [{"id": "tests", "label": "Tests"}]})
        board.add_message("t-1", {"kind": "notice", "code": "approval_request", "text": "check", "data": {
            "approval_id": "abc123", "capability": "chimera.review", "arguments": json.dumps({"fingerprint": "f1"})}})
        now = views.approval(board, board.task("t-1"))
        self.assertEqual((now["id"], now["remaining"], now["checklist"][0]["ticked"]), ("abc123", 1, False))
        self.assertEqual(views.actions(board.task("t-1"), now)[:3], ["send", "check", "approve"])
        board.update_task("t-1", status="working")                  # you asked for changes: not this version
        self.assertIsNone(views.approval(board, board.task("t-1")))
        board.update_task("t-1", status="review", data={"change": {"fingerprint": "f2"}})  # an older version's
        self.assertIsNone(views.approval(board, board.task("t-1")))
        failed = {"status": "failed", "data": {"origin_address": "webui:classroom"}}
        self.assertIn("report", views.actions(failed, None))
        failed["data"]["reported"] = {"to": "webui:classroom", "status": "failed"}
        self.assertNotIn("report", views.actions(failed, None))     # said already
        self.assertIn("report", views.actions({**failed, "status": "landed"}, None))  # a new outcome to tell
        lab = SimpleNamespace(state={"team": [], "tasks": [{"id": "t-1", "approval": now}]})
        session = cli.Session(lab, self.tmp, SimpleNamespace(json=True, crew="", workflow=None))
        session.task = "t-1"
        self.assertEqual(session.pending()["id"], "abc123")         # club asks about what the lab says
        session.asked.add("abc123")
        self.assertIsNone(session.pending())
        board.close()

    async def test_one_go_from_a_folder_adds_it_to_the_lab_works_and_stops_at_your_check(self):
        import io
        import shutil
        from extensions.chimera import cli
        club = await Club(self.tmp, DemoAdapter()).start()
        try:
            folder = self.tmp / "app"
            shutil.copytree(ROOT / "extensions" / "chimera" / "sample", folder)
            root = cli.workplace(folder, True, lambda text: None)
            out, err = io.StringIO(), io.StringIO()
            lab = await asyncio.to_thread(cli.Lab, club.page.url)
            try:
                session = cli.Session(lab, root, SimpleNamespace(json=True, crew="flash", workflow=None), out, err)
                self.assertTrue(await asyncio.to_thread(session.ensure_project))
                code = await asyncio.to_thread(cli.once, session, SUNDAY)
            finally:
                await asyncio.to_thread(lab.close)
            self.assertEqual(code, 0)
            result = json.loads(out.getvalue().splitlines()[-1])
            self.assertEqual((result["type"], result["status"], result["change"]), ("result", "review", "2 files (+14 −2)"))
            self.assertIn("message", {json.loads(line)["type"] for line in out.getvalue().splitlines()})
            project = club.parts["registry"].get("app")
            self.assertEqual(project.path, root)
            self.assertEqual(club.task(result["task"])["data"].get("crew"), None)  # flash is not on the demo team
            self.assertEqual(club.approvals(result["task"])[-1]["data"]["capability"], "chimera.review")  # yours
        finally:
            await club.stop()


if __name__ == "__main__":
    unittest.main()

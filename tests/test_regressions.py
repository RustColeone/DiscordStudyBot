"""Regression tests: every bug the review rounds found, as the behaviour it should have had. Each test names the
bug it guards against; the fix is in the code it exercises."""

import asyncio
import io
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from contracts import Address, Request, Sender
from contracts.models import AdapterResult, ToolCall
from tests.test_workbench import GREET, TEAM, VERIFY, Club, Scripted, criteria, git, route, turn

from extensions.workbench.board import Board
from extensions.workbench.demo import DemoAdapter
from extensions.workbench.workspace import tools
from extensions.workbench.workspace.projects import make_sample, project_from
from extensions.workbench.workspace.worktree import LandError, Worktrees

APPROVE = {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
REVISE = {"verdict": "revise", "say": "", "summary": "Not yet.", "issues": [
    {"severity": "blocker", "category": "correctness", "file": "NEW.md", "line": 1, "problem": "x", "suggestion": "y"}]}


def outcomes(club) -> list[str]:
    path = club.tmp / "data" / "outcomes.jsonl"
    return [json.loads(line)["event"] for line in path.read_text().splitlines()] if path.is_file() else []


def main_md(kind, system, messages, turns):
    """A change (MAIN.md), and questions answered without touching anything."""
    if kind == "route":
        return route(kind="question") if "Why" in messages[-1]["content"] else route()
    if kind == "review":
        return APPROVE
    if "asked a question" in system:
        return turn(status="done", summary="Because it is the main one.")
    return turn(("write_file", {"path": "MAIN.md", "content": "x\n"})) if turns == 0 else turn(status="done",
                                                                                              summary="MAIN.md")


class Lab(unittest.IsolatedAsyncioTestCase):
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

    async def start(self, club, text, **extra):
        """Submit the first request of a new task without waiting for it (for runs that block on purpose)."""
        task = f"t-held{time.time_ns() % 1000000}"
        club.board.create_task(task, f"workbench:{task}", "sample", text[:40], request=text)
        await club.submit(Request(id=f"first-{task}", address=Address("workbench", task), sender=Sender("creator", "Leo"),
                                  text=text, received_at=time.time(), display=club.page.display,
                                  extra={"task": task, "project": "sample", **extra}))
        return task


class FlowRegressions(Lab):
    async def test_a_question_while_the_change_waits_for_its_check_keeps_it_waiting(self):
        # Was: the question's answer set the task to `answered`; the old check card then approved a change no run
        # would land ("That approval was for an older version"), and the task was stuck.
        club = await self.club(Scripted(main_md), **TEAM)
        task = await club.new("Add MAIN.md.")
        self.assertEqual(club.task(task)["status"], "review")
        fingerprint = club.task(task)["data"]["change"]["fingerprint"]
        await club.say(task, "Why is it called MAIN?")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertEqual(state["data"]["change"]["fingerprint"], fingerprint)
        self.assertIn("still waiting for you", club.said(task)[-1][1])
        await club.decide(task, "workbench.review")
        await club.decide(task, "workbench.land")
        self.assertEqual(club.task(task)["status"], "landed")
        self.assertTrue((club.sample / "MAIN.md").is_file())

    async def test_a_question_queued_while_it_works_does_not_lose_the_change(self):
        # Was: only the last round reached finish(), so a queued question made the task `answered`, no change shown.
        started, release = asyncio.Event(), asyncio.Event()

        class Held(Scripted):
            async def complete(self, model, messages, schema, schema_name, *rest, **kwargs):
                if schema_name == "workbench_turn" and not started.is_set():
                    started.set()
                    await release.wait()
                return await super().complete(model, messages, schema, schema_name, *rest, **kwargs)
        club = await self.club(Held(main_md), **TEAM)
        task = await self.start(club, "Add MAIN.md.")
        await asyncio.wait_for(started.wait(), 10)
        await club.submit(Request(id="second", address=Address("workbench", task), sender=Sender("creator", "Leo"),
                                  text="Why is it called MAIN?", received_at=time.time(), display=club.page.display,
                                  extra={"task": task}))
        release.set()
        await asyncio.sleep(0.1)
        await club.idle()
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertEqual([item["path"] for item in state["data"]["change"]["files"]], ["MAIN.md"])
        self.assertEqual(len(club.approvals(task, "workbench.review")), 1)

    async def test_a_denied_second_check_is_recorded(self):
        # Was: `say` and `once` shared the journal key, so `check_denied` never reached outcomes.jsonl.
        club = await self.club(DemoAdapter())
        task = await club.new("Meetings on Sunday are listed first instead of last. Fix it and add a test.")
        await club.decide(task, "workbench.review", approve=False)
        self.assertEqual(outcomes(club)[-1], "check_denied")

    async def test_a_replay_takes_the_same_path_when_a_reviewer_recovers(self):
        # Was: a reviewer who failed was not journaled; on the replay after the approval she answered, the change was
        # revised into a new fingerprint, and the approved check no longer matched (the task could never land).
        healthy = {"now": False}

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return REVISE if healthy["now"] else "this is not JSON"
            if "Revise it once" in system:
                return turn(("write_file", {"path": "NEW.md", "content": "revised\n"})) if turns == 0 else \
                    turn(status="done", summary="revised")
            return turn(("write_file", {"path": "NEW.md", "content": "x\n"})) if turns == 0 else \
                turn(status="done", summary="NEW.md")
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Add NEW.md.", workflow="critique")
        fingerprint = club.task(task)["data"]["change"]["fingerprint"]
        self.assertEqual({step["id"]: step["status"] for step in club.board.steps(task)}["r1.review"], "failed")
        healthy["now"] = True
        await club.decide(task, "workbench.review")
        self.assertEqual(club.task(task)["data"]["change"]["fingerprint"], fingerprint)
        await club.decide(task, "workbench.land")
        self.assertEqual(club.task(task)["status"], "landed")

    async def test_a_named_fixer_who_gets_stuck_is_replaced_once_and_stays_replaced(self):
        # Was: the fallback asked the stuck seat again, announced it twice, and a replay flipped who finished it.
        class Flaky(Scripted):
            fails = 0

            async def complete(self, model, messages, schema, schema_name, *rest, **kwargs):
                turns = sum("Results of your actions" in message["content"] for message in messages)
                if model == "a-small" and schema_name == "workbench_turn" and "Revise it once" in \
                        messages[0]["content"] and turns >= 1:
                    Flaky.fails += 1
                    raise TimeoutError("too slow")
                return await super().complete(model, messages, schema, schema_name, *rest, **kwargs)

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route()
            if kind == "review":
                return REVISE
            if "Revise it once" in system:
                return turn(("write_file", {"path": "NEW.md", "content": "revised\n"})) if turns == 0 else \
                    turn(status="done", summary="revised")
            return turn(("write_file", {"path": "NEW.md", "content": "x\n"})) if turns == 0 else \
                turn(status="done", summary="NEW.md")
        club = await self.club(Flaky(answer), **TEAM)
        task = await club.new("Add NEW.md.", workflow="critique")
        stuck = [text for _, text in club.said(task) if "stuck" in text]
        self.assertEqual(len(stuck), 1)
        finished = club.task(task)["data"]["finished_by"]
        await club.decide(task, "workbench.review")
        self.assertEqual(club.task(task)["data"]["finished_by"], finished)
        self.assertEqual(Flaky.fails, 1)

    async def test_stopping_the_core_is_not_a_cancel(self):
        # Was: Manager.stop() cancelled the runs and the engine recorded them as cancelled; they resumed anyway.
        started = asyncio.Event()

        class Hangs(Scripted):
            async def complete(self, model, messages, schema, schema_name, *rest, **kwargs):
                if schema_name == "workbench_turn" and not started.is_set():
                    started.set()
                    await asyncio.sleep(3600)
                return await super().complete(model, messages, schema, schema_name, *rest, **kwargs)
        club = await self.club(Hangs(main_md), **TEAM)
        task = await self.start(club, "Add MAIN.md.")
        await asyncio.wait_for(started.wait(), 10)
        await club.stop()
        self.clubs.remove(club)
        self.assertNotIn("cancelled", outcomes(club))
        again = await self.club(Scripted(main_md), **TEAM)
        await again.idle()
        self.assertEqual(again.task(task)["status"], "review")
        self.assertNotIn("cancelled", outcomes(again))

    async def test_a_stopped_run_leaves_no_step_running(self):
        # Was: `$cancel` (or a failure) left the turn it cut off "running" for ever: a spinner on the page, and a
        # client waiting for the task's steps to finish never saw it end.
        started = asyncio.Event()

        class Hangs(Scripted):
            async def complete(self, model, messages, schema, schema_name, *rest, **kwargs):
                if schema_name == "workbench_turn":
                    started.set()
                    await asyncio.sleep(3600)
                return await super().complete(model, messages, schema, schema_name, *rest, **kwargs)
        club = await self.club(Hangs(main_md), **TEAM)
        task = await self.start(club, "Add MAIN.md.")
        await asyncio.wait_for(started.wait(), 10)
        await club.say(task, "$cancel")
        self.assertEqual(club.task(task)["status"], "cancelled")
        self.assertIn("skipped", [step["status"] for step in club.board.steps(task) if step["kind"] == "turn"])
        self.assertNotIn("running", [step["status"] for step in club.board.steps(task)])
        club.board.put_step(task, "r1.old", kind="turn")  # one left open by a lab from before the fix
        reopened = Board(club.board.path)
        self.assertEqual(reopened.steps(task)[-1]["status"], "skipped")
        reopened.close()

    async def test_a_change_asked_again_replaces_the_earlier_check_and_ends_its_own(self):
        # Was: after a question, the first second-check kept waiting beside the new one; and approving the new one
        # marked the change set reviewed, so the replay took present_again's "already checked" path and its step
        # never ended.
        club = await self.club(Scripted(main_md), **TEAM)
        task = await club.new("Add MAIN.md.")
        await club.say(task, "Why is it called MAIN?")
        checks = lambda: {step["id"]: step["status"] for step in club.board.steps(task) if step["kind"] == "approval"}
        self.assertEqual(checks(), {"r1.check": "skipped", "r2.check": "running"})
        await club.decide(task, "workbench.review")
        self.assertEqual(checks(), {"r1.check": "skipped", "r2.check": "done", "r2.land": "running"})

    async def test_a_turn_without_the_actions_field_is_a_turn_not_an_unreadable_answer(self):
        # Was: the turn schema required `actions`; DeepSeek's JSON mode often leaves the field out (35 of Flash's 393
        # calls in round 4), and each such reply was rejected, asked for again, and twice in a row handed to another.
        replies = iter([{"say": "", "note": "Looking first."},  # neither actions nor status: a turn used
                        turn(("write_file", {"path": "MAIN.md", "content": "x\n"})),
                        {"status": "done", "summary": "MAIN.md"}])  # done, with no actions field

        def answer(kind, system, messages, turns):
            return next(replies) if kind == "turn" else main_md(kind, system, messages, turns)
        adapter = Scripted(answer)
        club = await self.club(adapter, **TEAM)
        task = await club.new("Add MAIN.md.")
        self.assertEqual(club.task(task)["status"], "review")
        self.assertEqual([kind for kind, *_ in adapter.calls].count("turn"), 3)  # none was asked for again
        self.assertEqual([text for _, text in club.said(task) if "stuck" in text], [])

    async def test_a_seat_never_thinks_harder_than_her_limit(self):
        # Was: one turn_effort for everyone, so a cheap model that over-thinks at max could not be held lower while
        # the strong one kept max (round 4: Flash at max wrote ~25 times Sol's output tokens for the same task).
        adapter = Scripted(main_md)
        club = await self.club(adapter, **{**TEAM, "turn_effort": "max", "review_effort": "max",
                                           "team": {**TEAM["team"], "drafty": {**TEAM["team"]["drafty"], "effort": "high"}}})
        task = await club.new("Add MAIN.md.")
        self.assertEqual(club.task(task)["status"], "review")
        efforts = {(model, effort) for kind, model, effort in adapter.calls if kind in ("turn", "review")}
        self.assertEqual({effort for model, effort in efforts if model.startswith("a-")}, {"high"})  # Drafty's
        self.assertEqual({effort for model, effort in efforts if model.startswith("b-")}, {"max"})   # Checky's
        calls = [call for step in club.board.steps(task) for call in step["data"].get("calls") or ()]
        self.assertEqual({call["effort"] for call in calls if call["seat"] == "drafty"}, {"high"})

    async def test_a_finish_that_changed_nothing_is_questioned_once(self):
        # Was: a drafter that only described the work ("Added MAIN.md") and said done with an empty diff ended the
        # task as answered ("No files needed to change"), and in a cascade the judge never saw it (round 5: Flash at
        # low effort on the JSON parser).
        replies = iter([turn(status="done", summary="Added MAIN.md."),  # described, not done
                        turn(("write_file", {"path": "MAIN.md", "content": "x\n"})),
                        turn(status="done", summary="MAIN.md")])

        def answer(kind, system, messages, turns):
            return next(replies) if kind == "turn" else main_md(kind, system, messages, turns)
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Add MAIN.md.")
        self.assertEqual(club.task(task)["status"], "review")
        self.assertEqual([item["path"] for item in club.task(task)["data"]["change"]["files"]], ["MAIN.md"])

    async def test_nothing_to_change_said_twice_is_still_an_answer(self):
        def answer(kind, system, messages, turns):
            return turn(status="done", summary="Nothing needs to change.") if kind == "turn" else \
                main_md(kind, system, messages, turns)
        adapter = Scripted(answer)
        club = await self.club(adapter, **TEAM)
        task = await club.new("Make sure MAIN.md is not needed.")
        self.assertEqual(club.task(task)["status"], "answered")
        self.assertEqual([kind for kind, *_ in adapter.calls].count("turn"), 2)  # questioned once, not again

    async def test_a_replayed_turn_keeps_the_effort_it_was_answered_with(self):
        # Was: when Checky took over from a stuck Drafty, the replay re-recorded Drafty's turns with Checky's effort
        # (round 5: Flash capped at high showed "max" in every run Sol took over).
        class StuckSecondTurn(Scripted):
            async def complete(self, model, messages, schema, schema_name, *rest, **kwargs):
                turns = sum("Results of your actions" in message["content"] for message in messages)
                if model == "a-small" and schema_name == "workbench_turn" and turns >= 1:
                    return AdapterResult("not json", 10, 10)
                return await super().complete(model, messages, schema, schema_name, *rest, **kwargs)
        club = await self.club(StuckSecondTurn(main_md), **{**TEAM, "turn_effort": "max", "team": {
            **TEAM["team"], "drafty": {**TEAM["team"]["drafty"], "effort": "high"}}})
        task = await club.new("Add MAIN.md.", workflow="single", crew=["drafty"])
        calls = [call for step in club.board.steps(task) for call in step["data"].get("calls") or ()]
        self.assertIn("checky", {call["seat"] for call in calls})  # she took over
        self.assertEqual({call["effort"] for call in calls if call["seat"] == "drafty" and call["role"] != "router"},
                         {"high"})

    async def test_cancelling_a_run_that_waits_for_an_approval_stops_the_task(self):
        # Was: the core cancelled the waiting run, but no engine code was running to hear it, so the task stayed
        # "working · waiting for you" with its check step spinning (round 4: a critique run left after its limit).
        club = await self.club(Scripted(main_md), **TEAM)
        task = await club.new("Add MAIN.md.")
        self.assertEqual(club.task(task)["status"], "review")  # waiting for the second check
        await club.say(task, "$cancel")
        self.assertEqual(club.task(task)["status"], "cancelled")
        self.assertNotIn("running", [step["status"] for step in club.board.steps(task)])
        self.assertIn("cancelled", outcomes(club))

    def native_club(self, turns):
        """Drafty works through tool calls (scripted: each turn's words and calls); everyone else as before."""
        seen = []

        class Native(Scripted):
            supports_tools = True

            async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=(),
                               tools=()):
                if not tools:
                    return await super().complete(model, messages, schema, schema_name, effort, max_tokens, timeout_s,
                                                  images)
                seen.append({"messages": messages, "tools": [tool.name for tool in tools]})
                text, calls = turns.pop(0)
                return AdapterResult(text, 10, 5, tool_calls=tuple(ToolCall(f"c{len(seen)}.{index}", name, arguments)
                                                                   for index, (name, arguments) in enumerate(calls)),
                                     replay={"reasoning_content": f"thought {len(seen)}"})
        config = {**TEAM, "team": {**TEAM["team"], "drafty": {**TEAM["team"]["drafty"], "tool_calls": True}}}
        return Native(main_md), config, seen

    async def test_a_classmate_with_tool_calls_works_through_her_own_calls(self):
        # Native tool calls (team.<seat>.tool_calls): her calls are the actions, finish ends the work, and her own
        # messages go back with their replay (DeepSeek's thinking mode refuses a request without it).
        adapter, config, seen = self.native_club([
            ("Writing MAIN.md.", [("write_file", {"path": "MAIN.md", "content": "x\n"})]),
            ("", [("finish", {"status": "done", "summary": "Added MAIN.md."})])])
        club = await self.club(adapter, **config)
        task = await club.new("Add MAIN.md.", workflow="single", crew=["drafty"])
        self.assertEqual(club.task(task)["status"], "review")
        self.assertEqual([item["path"] for item in club.task(task)["data"]["change"]["files"]], ["MAIN.md"])
        self.assertIn("Work by calling the tools", seen[0]["messages"][0]["content"])
        self.assertIn("finish", seen[0]["tools"])
        mine, result = seen[1]["messages"][2], seen[1]["messages"][3]
        self.assertEqual((mine["role"], mine["content"], mine["replay"]), ("assistant", "Writing MAIN.md.",
                                                                             {"reasoning_content": "thought 1"}))
        self.assertEqual(mine["tool_calls"], [{"id": "c1.0", "name": "write_file",
                                               "arguments": {"path": "MAIN.md", "content": "x\n"}}])
        self.assertEqual((result["role"], result["tool_call_id"]), ("tool", "c1.0"))
        self.assertTrue(result["content"].startswith("ok"))
        turn = [step for step in club.board.steps(task) if step["id"] == "r1.single.1"][0]
        self.assertEqual(turn["data"]["note"], "Writing MAIN.md.")

    async def test_a_turn_that_only_updates_the_plan_gets_no_reminder(self):
        # Was: the plan-only turn counted as "no tools ran" and got the reminder meant for a turn that did nothing
        # (seen in the first live run with DeepSeek).
        adapter, config, seen = self.native_club([
            ("", [("plan", {"steps": [{"step": "Write MAIN.md", "done": False}]})]),
            ("", [("write_file", {"path": "MAIN.md", "content": "x\n"})]),
            ("", [("finish", {"status": "done", "summary": "Added MAIN.md."})])])
        club = await self.club(adapter, **config)
        task = await club.new("Add MAIN.md.", workflow="single", crew=["drafty"])
        self.assertEqual(club.task(task)["status"], "review")
        after_plan = seen[1]["messages"][3:]
        self.assertEqual([(message["role"], message["content"]) for message in after_plan], [("tool", "noted")])
        self.assertEqual(club.task(task)["data"]["plan"], [{"step": "Write MAIN.md", "done": False}])

    async def test_a_finish_beside_other_calls_stands_once_they_worked(self):
        adapter, config, seen = self.native_club([
            ("", [("write_file", {"path": "MAIN.md", "content": "x\n"}),
                  ("finish", {"status": "done", "summary": "Added MAIN.md."})])])
        club = await self.club(adapter, **config)
        task = await club.new("Add MAIN.md.", workflow="single", crew=["drafty"])
        self.assertEqual((club.task(task)["status"], len(seen)), ("review", 1))

    async def test_a_crew_member_takes_the_work_even_when_no_role_lists_her(self):
        # Was: the crew only reordered a role's own list, so a classmate chosen as crew but listed for no role never
        # worked; the role's first seat did (the polyglot pilot: every "native tools" run was the JSON seat's).
        adapter = Scripted(main_md)
        team = {**TEAM["team"], "solo": {"name": "Solo", "family": "gamma", "cheap": ["alpha/a-solo"]}}
        club = await self.club(adapter, **{**TEAM, "team": team})
        task = await club.new("Add MAIN.md.", workflow="single", crew=["solo"])
        self.assertEqual(club.task(task)["status"], "review")
        self.assertEqual({step["seat"] for step in club.board.steps(task) if step["kind"] == "turn"}, {"solo"})
        self.assertIn("a-solo", {model for kind, model, effort in adapter.calls if kind == "turn"})

    async def test_a_lab_confined_to_the_worktree_still_runs_git_and_the_tests(self):
        import shutil
        if not shutil.which("bwrap"):
            self.skipTest("no bubblewrap here")
        replies = iter([turn(("write_file", {"path": "MAIN.md", "content": "x\n"}),
                             ("run", {"command": "git status --short; ls /home | wc -l"})),
                        turn(status="done", summary="MAIN.md")])

        def answer(kind, system, messages, turns):
            return next(replies) if kind == "turn" else main_md(kind, system, messages, turns)
        club = await self.club(Scripted(answer), **{**TEAM, "command_filesystem": "worktree",
                                                     "command_network": False})
        task = await club.new("Add MAIN.md.", workflow="single", crew=["drafty"])
        await club.decide(task, "workbench.run")  # (`ls /home` asks first: approved, it runs confined all the same)
        self.assertEqual(club.task(task)["status"], "review")
        ran = [action for step in club.board.steps(task) for action in step["data"].get("actions") or ()
               if action["tool"] == "run"][0]
        self.assertIn("?? MAIN.md", ran["summary"])  # git works inside: the project's git store is shared
        self.assertIn("\n0", ran["summary"])  # and /home is empty
        tests = [step for step in club.board.steps(task) if step["kind"] == "validate" and step["id"] != "r1.baseline"]
        self.assertTrue(tests and tests[-1]["status"] == "done")

    async def test_continue_without_actions_is_a_turn_not_the_end(self):
        # Was: {"status": "continue", "actions": []} ended the work as "ran out of turns".
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="tiny")
            text = "\n".join(str(message["content"]) for message in messages)
            if turns:
                return turn(status="done", summary="MAIN.md")
            if "No actions ran" in text:
                return turn(("write_file", {"path": "MAIN.md", "content": "x\n"}))
            return turn()  # continue, and nothing to do yet
        club = await self.club(Scripted(answer), **TEAM)
        task = await club.new("Add MAIN.md.", workflow="single")
        self.assertEqual(club.task(task)["status"], "review")

    async def test_a_repair_that_breaks_the_tests_puts_back_the_tested_change(self):
        # Was: a failed repair turned a passing change with one finding into a failed task.
        original = (Path(__file__).resolve().parents[1] / "extensions/workbench/sample/clubnotes/schedule.py").read_text()

        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            if kind == "requirements":
                return criteria()
            if kind == "review":
                return APPROVE
            if "the club's tester" in system:
                return {**turn(status="done", summary="Probed."),
                        "findings": [{"criterion": "R1", "problem": "greet() says hi", "evidence": "printed hi"}]}
            if "tester tried to break" in system:  # the repair breaks the project
                return turn(("write_file", {"path": "clubnotes/schedule.py", "content": "broken(\n"})) if turns == 0 \
                    else turn(status="done", summary="Fixed it, I think.")
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hi'\n"})) \
                if turns == 0 else turn(status="done", summary="greet() added.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="single")
        state = club.task(task)
        self.assertEqual(state["status"], "review")
        self.assertEqual([item["path"] for item in state["data"]["change"]["files"]], ["clubnotes/greet.py"])
        self.assertEqual((Path(state["data"]["worktree"]) / "clubnotes/schedule.py").read_text(), original)
        self.assertIn("didn't pass", state["data"]["warning"])

    async def test_the_probe_count_survives_a_replay(self):
        # Was: attempt 0's prefix `r1.probe.` also counted attempt 1's steps `r1.probe.1.N` on the replay.
        def answer(kind, system, messages, turns):
            if kind == "route":
                return route(size="small")
            if kind == "requirements":
                return criteria()
            if kind == "review":
                return APPROVE
            text = "\n".join(str(message["content"]) for message in messages)
            if "the club's tester" in system:
                if turns == 0:
                    return turn(("probe_file", {"path": "greet.py", "content": "import sys\nsys.path.insert(0, '.')\n"
                                                "from clubnotes.greet import greet\nprint(greet())\n"}),
                                ("probe", {"command": "python3 .probe/greet.py"}))
                fixed = "hello" in text.split("## New code", 1)[-1].split("## Old code", 1)[0]
                return {**turn(status="done", summary="Probed."), "findings": [] if fixed else [
                    {"criterion": "R1", "problem": "greet() says hi", "evidence": "printed hi"}]}
            if "tester tried to break" in system:
                return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hello'\n"})) \
                    if turns == 0 else turn(status="done", summary="Now hello.")
            return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hi'\n"})) \
                if turns == 0 else turn(status="done", summary="greet() added.")
        club = await self.club(Scripted(answer), **VERIFY)
        task = await club.new(GREET, workflow="single")
        before = club.task(task)["data"]["verification"]["probes"]
        await club.decide(task, "workbench.review")
        self.assertEqual(club.task(task)["data"]["verification"]["probes"], before)


class GateRegressions(Lab):
    async def test_review_land_and_discard_follow_the_task_status(self):
        # Was: `review` passed for a failed or landed task with no checklist; discard ran while a model worked.
        club = await self.club(DemoAdapter())
        w = club.page.workbench
        club.board.create_task("t-gate01", "workbench:t-gate01", "sample", "Gate", request="x")
        context = SimpleNamespace(address=Address("workbench", "t-gate01"))
        for status in ("failed", "landed", "answered", "working"):
            club.board.update_task("t-gate01", status=status, data={"change": {"fingerprint": "f1"}})
            result = await w.review({"task": "t-gate01", "fingerprint": "f1"}, context)
            self.assertEqual(result.error_code, "not_in_review", status)
        club.board.update_task("t-gate01", status="review")
        self.assertEqual((await w.review({"task": "t-gate01", "fingerprint": "f1"}, context)).error_code, "no_checklist")
        club.board.update_task("t-gate01", status="answered")
        self.assertEqual((await w.land({"task": "t-gate01", "fingerprint": "f1"}, context)).error_code, "not_in_review")
        club.board.update_task("t-gate01", status="working")
        self.assertEqual((await w.discard({"task": "t-gate01"}, context)).error_code, "busy")

    async def test_a_tick_from_an_older_version_and_a_blank_invitation_are_refused(self):
        club = await self.club(DemoAdapter())
        task = await club.new("Meetings on Sunday are listed first instead of last. Fix it and add a test.")
        with self.assertRaisesRegex(ValueError, "older version"):  # (the module's own ToolError, loaded by the core)
            club.page.workbench.tick(club.task(task), ["all"], True, fingerprint="not-this-one")
        invite = await club.page.workbench.invite({"brief": "   "}, SimpleNamespace(address=Address("webui", "c")))
        self.assertEqual(invite.error_code, "no_brief")

    async def test_an_approval_decided_elsewhere_is_not_offered_and_a_report_counts_once_delivered(self):
        # Was: a typed `$approve` never closed its card (the page kept offering it), and `reported` was written when
        # the report was submitted, not when it arrived.
        from extensions.workbench import views
        club = await self.club(DemoAdapter())
        page, board = club.page, club.board
        task = await club.new("Meetings on Sunday are listed first instead of last. Fix it and add a test.")
        await club.say(task, f"$check {task} all")
        review = club.approvals(task, "workbench.review")[-1]["data"]["approval_id"]
        await page._event(None, {"type": "send", "task": task, "text": f"$approve {review}", "id": "typed"})
        await asyncio.sleep(0.2)
        await club.idle()
        land = club.approvals(task, "workbench.land")[-1]["data"]["approval_id"]
        await page._event(None, {"type": "send", "task": task, "text": f"$deny {land}", "id": "deny", "action": "Deny"})
        await asyncio.sleep(0.2)
        await club.idle()
        self.assertIsNone(page._task_view(club.task(task))["approval"])  # neither the typed one nor the denied one
        board.create_task("t-rep001", "workbench:t-rep001", "sample", "Q", request="q",
                          origin_address="webui:classroom", answer="42")
        board.update_task("t-rep001", status="answered")
        await page._event(None, {"type": "report", "task": "t-rep001"})
        await asyncio.sleep(0.3)
        await club.idle()
        self.assertIsNone(board.task("t-rep001")["data"].get("reported"))  # (no classroom here: not delivered)
        self.assertIn("report", views.actions(board.task("t-rep001"), None))

    async def test_a_page_cannot_send_into_a_task_owned_by_another_conversation(self):
        club = await self.club(DemoAdapter())
        club.board.create_task("t-else01", "discord:123", "sample", "Elsewhere", request="x")
        told = []
        club.page._tell = lambda page, message: told.append(message)
        await club.page._send_text(None, "t-else01", "take it over", "c1", "")
        self.assertEqual(told[-1]["reason"], "not_here")


class PageRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.club = await Club(Path(self._tmp.name), DemoAdapter(), public_url="https://lab.example.org").start()
        self.url = f"http://127.0.0.1:{self.club.page.port_number}"

    async def asyncTearDown(self):
        await self.club.stop()
        self._tmp.cleanup()

    async def test_a_task_loads_over_the_socket_in_order_and_a_proxy_origin_is_accepted(self):
        import aiohttp
        task = await self.club.new("Meetings on Sunday are listed first instead of last. Fix it and add a test.")
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(self.url + "/ws", headers={"Origin": "https://lab.example.org"}) as ws:
                self.assertEqual((await ws.receive_json())["type"], "hello")
                await ws.send_json({"type": "load", "task": task})
                while (message := await asyncio.wait_for(ws.receive_json(), 10))["type"] != "loaded":
                    pass
                self.assertEqual(message["task"]["id"], task)
                self.assertTrue(message["steps"] and message["messages"])
            refused = await http.get(self.url + "/", headers={"Origin": "https://evil.example"})
            self.assertEqual(refused.status, 403)


class WorkspaceRegressions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.repo = make_sample(self.tmp / "sample")
        self.project = project_from("sample", {"path": str(self.repo)})
        self.trees = Worktrees(self.tmp / "worktrees")

    def tearDown(self):
        self._tmp.cleanup()

    async def test_what_is_staged_or_committed_in_the_worktree_never_changes_what_lands(self):
        # Was: the change set was read from a copy of the worktree's index, and land assumed HEAD was the base.
        opened = await self.trees.open(self.project, "t-1")
        path = Path(opened["path"])
        tools.write_file(path, "NOTES.md", "new\n")
        before = await self.trees.change(path, opened["base"])
        git(path, "add", "NOTES.md")
        git(path, "-c", "user.email=x@y", "-c", "user.name=x", "commit", "-qm", "a model's own commit")
        tools.write_file(path, "MORE.md", "more\n")
        change = await self.trees.change(path, opened["base"])
        self.assertEqual({item["path"] for item in change.files}, {"NOTES.md", "MORE.md"})
        self.assertNotEqual(before.fingerprint, change.fingerprint)
        landed = await self.trees.land(self.project, path, opened["branch"], opened["base"], "Notes", change.fingerprint)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD~1"), opened["base"])  # one commit, on the base
        self.assertEqual(git(self.repo, "show", "--name-only", "--format=", "HEAD").split(), ["MORE.md", "NOTES.md"])
        self.assertEqual(landed["commit"], git(self.repo, "rev-parse", "HEAD"))

    async def test_a_refused_land_leaves_the_task_landable(self):
        opened = await self.trees.open(self.project, "t-2")
        path = Path(opened["path"])
        tools.write_file(path, "README.md", "changed by the task\n")
        change = await self.trees.change(path, opened["base"])
        (self.repo / "README.md").write_text("your own edit\n")
        with self.assertRaisesRegex(LandError, "uncommitted"):
            await self.trees.land(self.project, path, opened["branch"], opened["base"], "x", change.fingerprint)
        git(self.repo, "checkout", "--", "README.md")
        await self.trees.land(self.project, path, opened["branch"], opened["base"], "x", change.fingerprint)
        self.assertEqual((self.repo / "README.md").read_text(), "changed by the task\n")

    async def test_restore_puts_a_worktree_back_to_an_earlier_change_set(self):
        opened = await self.trees.open(self.project, "t-3")
        path = Path(opened["path"])
        tools.write_file(path, "NOTES.md", "good\n")
        good = await self.trees.change(path, opened["base"])
        tools.write_file(path, "NOTES.md", "worse\n")
        tools.write_file(path, "EXTRA.md", "noise\n")
        tools.delete_file(path, "README.md")
        back = await self.trees.restore(path, opened["base"], good.tree)
        self.assertEqual(back.fingerprint, good.fingerprint)
        self.assertFalse((path / "EXTRA.md").exists())
        self.assertTrue((path / "README.md").exists())

    def test_file_tools_keep_links_line_endings_and_the_git_folder(self):
        target = self.repo / "README.md"
        (self.repo / "link.md").symlink_to(target)
        tools.delete_file(self.repo, "link.md")  # the link goes, never the file it points at
        self.assertTrue(target.exists() and not (self.repo / "link.md").exists())
        with self.assertRaises(tools.ToolError):
            tools.inside(self.repo, ".GIT/config")
        (self.repo / "dos.txt").write_bytes(b"one\r\ntwo\r\n")
        tools.edit_file(self.repo, "dos.txt", "one\ntwo", "uno\ndos")
        self.assertEqual((self.repo / "dos.txt").read_bytes(), b"uno\r\ndos\r\n")

    def test_harmless_lines_from_the_cost_experiment_run_without_asking(self):
        # Was: `2>/dev/null` counted as a path outside the copy, and every heredoc asked, so 4 of 27 runs stopped
        # for a person. A heredoc with a quoted marker is only text for the program; an unquoted one still asks.
        from extensions.workbench.workspace.commands import allowed
        self.assertTrue(allowed("python3 -m unittest discover -q 2>&1 | tail -3; git -C . diff --stat 2>/dev/null"))
        self.assertTrue(allowed("python3 - <<'PY'\nimport sys\nprint(sys.argv)\nPY"))
        self.assertTrue(allowed("cat <<'EOF' > notes.txt\n$(whoami)\nEOF"))  # (nothing is expanded in it)
        self.assertFalse(allowed("python3 - <<PY\nprint($(whoami))\nPY"))
        self.assertFalse(allowed("python3 - <<'PY'\nprint(1)"))  # no end marker
        self.assertFalse(allowed("cat x > /tmp/y"))
        self.assertFalse(allowed("ls 2>/etc/passwd"))

    def test_probes_compare_what_the_code_does_not_where_or_how_fast(self):
        from extensions.workbench.capabilities import comparable
        new, old = Path("/w/t-1"), Path("/w/t-1.base")
        self.assertEqual(comparable("ran /w/t-1/a.py in 0.31s at 0x7f00aa11bb", new),
                         comparable("ran /w/t-1.base/a.py in 0.07s at 0x7f00cc22dd", old))


class IsolatedCommands(unittest.IsolatedAsyncioTestCase):
    """Was: a tester tried to curl an exercise's hidden tests from GitHub (the allowlist held it), and others read them
    from the same machine (`sed`, `cp` and `find /` count as looking around), or reached a sibling setup's branch
    through git. `command_network: false` takes the network away; `command_filesystem: worktree` shows a command only
    the system, its worktree and its project."""

    def setUp(self):
        from extensions.workbench.workspace import system
        self.system = system

    async def test_commands_can_run_without_network(self):
        if self.system._offline_prefix() is None:
            self.skipTest("no user namespaces here")
        with tempfile.TemporaryDirectory() as folder:
            line = ("python3 -c \"import socket; socket.create_connection(('192.0.2.1', 53), timeout=3)\" "
                    "2>&1 | tail -1; touch made.txt")
            text, data = await tools.run(Path(folder), line, 30, self.system.Isolation(network=False))
            self.assertIn("Network is unreachable", text)
            self.assertTrue((Path(folder) / "made.txt").is_file())  # its own files are still hers to write

    async def test_a_confined_command_sees_its_worktree_and_nothing_else_of_ours(self):
        import shutil
        if not shutil.which("bwrap"):
            self.skipTest("no bubblewrap here")
        with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as other:
            (Path(other) / "answers.txt").write_text("the hidden tests\n")
            line = (f"cat {other}/answers.txt 2>&1 | tail -1; ls /home | wc -l; test -e /proc/{os.getpid()} && "
                    "echo 'sees the lab' || echo 'no lab process'; echo mine > made.txt")
            text, data = await tools.run(Path(work), line, 30,
                                         self.system.Isolation(network=False, confined=True))
            self.assertIn("No such file or directory", text)
            self.assertNotIn("the hidden tests", text)
            self.assertIn("\n0\n", text)  # /home is empty
            self.assertIn("no lab process", text)
            self.assertEqual((Path(work) / "made.txt").read_text(), "mine\n")

    async def test_isolation_where_it_cannot_be_had_refuses_the_command(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            for isolation, target, value in ((self.system.Isolation(network=False), "_offline_prefix", lambda: None),
                                             (self.system.Isolation(confined=True), "shutil", SimpleNamespace(
                                                 which=lambda name: None))):
                with patch.object(self.system, target, value):
                    with self.assertRaises(tools.ToolError):
                        await tools.run(Path(folder), "touch made.txt", 30, isolation)
                self.assertFalse((Path(folder) / "made.txt").exists())


class CliRegressions(unittest.TestCase):
    def session(self, task):
        from extensions.workbench import cli
        lab = SimpleNamespace(state={"team": [], "tasks": [task], "projects": []}, events=queue.Queue(), url="http://x",
                              sent=[])
        lab.send = lab.sent.append
        session = cli.Session(lab, None, SimpleNamespace(json=False, crew="", workflow=None), io.StringIO(),
                              io.StringIO())
        session.task = task["id"]
        return session, lab

    def test_an_approval_left_for_later_is_asked_again_and_never_hangs(self):
        # Was: Enter put the approval in `asked`, so /resume on a working task looped forever without asking.
        approval = {"id": "abc123", "capability": "workbench.run", "summary": "run pytest", "command": "pytest"}
        session, _ = self.session({"id": "t-1", "status": "working", "data": {}, "approval": approval, "own": True})
        asked = []
        session.prompt = lambda text: asked.append(text) or ""
        self.assertEqual(session.follow(True), "working")
        done = []
        thread = threading.Thread(target=lambda: done.append(session.follow(True)), daemon=True)
        thread.start()
        thread.join(5)
        self.assertEqual((done, len(asked)), (["working"], 2))

    def test_a_typo_at_the_final_approval_is_not_a_denial(self):
        # Was: anything but y/yes denied the landing (`a`, `approve`, `ok` included).
        approval = {"id": "abc123", "capability": "workbench.land", "summary": "land it"}
        session, lab = self.session({"id": "t-1", "status": "approved", "data": {}, "approval": approval, "own": True})
        answers = iter(["yse", "approve"])
        session.prompt = lambda text: next(answers)
        self.assertTrue(session.decide(approval))
        self.assertEqual(lab.sent[-1]["text"], "$approve abc123")

    def test_a_lab_that_closes_the_connection_is_an_error_the_session_handles(self):
        from extensions.workbench import cli
        session, lab = self.session({"id": "t-1", "status": "working", "data": {}, "own": True})
        lab.events.put({"type": "closed"})
        with self.assertRaises(cli.LabClosed):
            session.follow(False)


class LauncherRegressions(unittest.TestCase):
    def arguments(self, **values):
        return SimpleNamespace(**{"data": None, "config": [], "port": None, "host": None, "demo": False, "key": [],
                                  "env": [], "also": [], "core": [], **values})

    def test_only_the_keys_you_name_reach_the_core(self):
        # Was: every key exported in the shell went to the core with the named ones.
        from extensions.workbench.__main__ import handed_environment
        environment = handed_environment({"DEEPSEEK_API_KEY": "named"}, {"PATH": "/bin", "OPENAI_API_KEY": "shell",
                                                                         "DISCORD_TOKEN": "t", "HOME": "/h"})
        self.assertEqual(environment, {"PATH": "/bin", "HOME": "/h", "DEEPSEEK_API_KEY": "named"})

    def test_a_core_file_keeps_the_lab_its_paths_and_its_dates(self):
        # Was: a --core module list dropped the workbench, its paths were read from <data>/core/, a partial
        # `assignments` left `*` on the unloaded assistant, and a YAML date crashed json.dumps.
        import yaml
        from extensions.workbench.__main__ import HERE, core_config
        with tempfile.TemporaryDirectory() as folder:
            extra = Path(folder) / "extra.yml"
            extra.write_text("modules: {paths: [mine]}\nassignments: {'webui:*': {engine: room}}\n"
                             "extension_config: {room: {models_since: 2026-06-01}}\n")
            config, _ = core_config(self.arguments(data=folder, core=[str(extra)]))
        self.assertIn(str(HERE), config["modules"]["paths"])
        self.assertIn(str(Path(folder).resolve() / "mine"), config["modules"]["paths"])
        self.assertEqual(config["assignments"]["*"]["engine"], "workbench")
        self.assertIn("webui:*", config["assignments"])
        dumped = yaml.safe_load(yaml.safe_dump(config))
        self.assertEqual(str(dumped["extension_config"]["room"]["models_since"]), "2026-06-01")


class CoreRegressions(unittest.TestCase):
    def test_publish_takes_only_plain_json_and_the_place_shape(self):
        from core.gateway.gateway import Gateway
        gateway = Gateway.__new__(Gateway)
        gateway._published = {}
        with self.assertRaises(ValueError):
            gateway.publish("lab", "javascript://alert(1)", None)
        with self.assertRaises(ValueError):
            gateway.publish("lab", None, {"place": {"name": "Lab"}, "load": float("nan")})
        with self.assertRaises(ValueError):
            gateway.publish("lab", None, {"place": {"name": "Lab"}, "people": "everyone"})
        with self.assertRaises(ValueError):
            gateway.publish("lab", None, {"when": object()})
        gateway.publish("lab", "https://lab.example", {"place": {"name": "Lab"}, "people": [], "tickets": []})
        self.assertEqual(gateway._published["lab"]["link"], "https://lab.example")

    def test_people_called_on_in_the_middle_are_addressed(self):
        # Was: only the first and last segments counted, so "Luna, Gemini, what do you think?" left Gemini out.
        from extensions.room.engine import RoomEngine
        person = lambda name, shown: SimpleNamespace(name=name, display_name=shown)
        session = SimpleNamespace(personas=[person("luna", "Luna"), person("gemini", "Gemini"), person("lulu", "露娜")])
        engine = RoomEngine(aliases={})
        for text in ("Luna, Gemini, what do you think?", "Luna, check v1.2. Gemini, check v1.3.",
                     "Ok Luna, so Gemini, what?", "Luna.\n\nGemini, thoughts?", "露娜，Gemini，帮我看看"):
            addressed, _ = engine.participants(session, text)
            self.assertIn("gemini", addressed, text)
        self.assertEqual(engine.participants(session, "Luna, what did Gemini and I do?"), (["luna"], ["gemini"]))

    def test_an_evidence_judge_sends_back_only_what_it_can_show_failing(self):
        # Found by measuring: both cascade send-backs in the cost experiment were correct drafts (all hidden tests
        # passed) sent back over a minor requirement or scope note. With minor_needs_failing_input, such a note blocks
        # only when it names an input that fails; blockers and major issues always block.
        from extensions.workbench.kernel.review import Verdict
        verdict = Verdict(False, "", [
            {"severity": "minor", "category": "scope", "problem": "an unasked capacity check"},
            {"severity": "minor", "category": "correctness", "problem": "off by one", "failing_input": "f([1])"},
            {"severity": "major", "category": "style", "problem": "unreadable"}])
        self.assertEqual(len(verdict.blocking()), 3)
        self.assertEqual([issue["problem"] for issue in verdict.blocking(evidence=True)], ["off by one", "unreadable"])

    def test_cached_prompt_tokens_are_read_from_both_providers(self):
        # Was: only the total was kept, so a cost estimate priced cached input (20x cheaper at OpenAI, 50x at
        # DeepSeek) as fresh input.
        from core.providers.adapters import cached_tokens
        openai = SimpleNamespace(prompt_tokens=1000, prompt_tokens_details=SimpleNamespace(cached_tokens=640))
        deepseek = SimpleNamespace(prompt_tokens=1000, prompt_cache_hit_tokens=512, prompt_cache_miss_tokens=488)
        self.assertEqual((cached_tokens(openai), cached_tokens(deepseek), cached_tokens(None)), (640, 512, 0))
        from contracts import ModelResult
        self.assertEqual(ModelResult("x", cached_input_tokens=3).cached_input_tokens, 3)

    def test_a_modules_provider_settings_override_the_built_in_ones_but_not_core_yml(self):
        # Was: the built-in provider settings were merged into the same table as core.yml's, which "wins key by key"
        # over a module's suggestion, so the lab's `providers: deepseek: {schema_mode: ...}` never applied.
        from core.config import CoreConfig
        from core.providers.registry import Providers
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "core.yml"
            path.write_text("env_files: []\nproviders:\n  deepseek: {base_url: 'https://owner.example'}\n")
            config = CoreConfig.load(path)
        self.assertEqual(config.configured_providers, {"deepseek": {"base_url": "https://owner.example"}})
        providers = Providers(config.providers, {}, configured=config.configured_providers)
        providers.add_provider("deepseek", {"schema_mode": "strict_tool", "base_url": "https://module.example"})
        settings = providers._settings["deepseek"]
        self.assertEqual((settings["schema_mode"], settings["base_url"]), ("strict_tool", "https://owner.example"))
        self.assertEqual(settings["api_key_env"], "DEEPSEEK_API_KEY")  # the built-in keys nobody overrode stay

    def test_every_lab_schema_has_a_strict_tool_form(self):
        # Was: the last turn's `actions` was an array without `items`; strict tool mode drops `maxItems`, and DeepSeek
        # refused the bare array (400), so Sol took over the tester's last turn in every run that reached it.
        from core.providers.adapters import strict_tool_schema
        from extensions.workbench.kernel import prompts

        def bad(schema, path="schema"):
            if not isinstance(schema, dict):
                return []
            found = []
            if schema.get("type") == "array" and not isinstance(schema.get("items"), dict):
                found.append(f"{path}: an array without items")
            if schema.get("type") == "object" and schema.get("properties") is not None and \
                    schema.get("required") != list(schema["properties"]):
                found.append(f"{path}: not every property required")
            for name, value in (schema.get("properties") or {}).items():
                found += bad(value, f"{path}.{name}")
            return found + bad(schema.get("items"), f"{path}[]")
        turn = prompts.turn_schema(prompts.WRITE_TOOLS)
        last = prompts.last_turn(turn)
        schemas = {"turn": turn, "last turn": last, "probe": prompts.turn_schema(prompts.PROBE_TOOLS, findings=True),
                   **{name: value for name, value in vars(prompts).items() if name.endswith("_SCHEMA")}}
        for name, schema in schemas.items():
            self.assertEqual(bad(strict_tool_schema(schema), name), [])

    def test_place_keys_work_for_names_in_any_script(self):
        from extensions.workbench.places import key
        self.assertEqual((key("Flash-chan"), key("露娜"), key("")), ("flashchan", "露娜", ""))


if __name__ == "__main__":
    unittest.main()

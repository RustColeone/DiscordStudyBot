"""The lab's conformance kit: scenarios as data, so any implementation of the lab (another language, another
platform) can prove it behaves the same.

A scenario (tests/conformance/<name>.json) holds:
- `config`: the lab's settings;
- `inputs`: what the person does, in order: `{"new": text, "workflow": name}`, `{"say": text}`, `{"check": "all"}`,
  `{"approve": capability}`, `{"deny": capability}`;
- `answers`: every model answer, in the order the lab asks (`schema` names the JSON schema it answers);
- `expect`: the observable trace: the task's final status and workflow, every step (id, kind, status), every line
  said (who, what), the approvals requested, the outcomes logged, the commits on the project's branch and the files
  of the change set. Numbers that vary between runs (timings, commit hashes) are normalized.

Because a run is a pure function of its journal (SPEC.md §3), the same answers in the same order must give the same
trace. `record` makes the files from scripted scenarios; tests/test_conformance.py plays them back.

    venv/bin/python -m tests.conformance_kit record     # rewrite tests/conformance/*.json
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from contracts import AdapterResult

HERE = Path(__file__).resolve().parent / "conformance"
NUMBERS = [(re.compile(r"in \d+\.\d+s"), "in N.Ns"), (re.compile(r"\b[0-9a-f]{7,40}\b"), "<hash>"),
           (re.compile(r"\b\d+\.\d+s\b"), "N.Ns"), (re.compile(r"\bt-[a-z0-9]{4,40}\b"), "<task>"),
           (re.compile(r"(?<=Approved )[0-9a-f]{6}\b|(?<=\$approve )[0-9a-f]{6}\b|(?<=\$deny )[0-9a-f]{6}\b"),
            "<approval>")]


def normalize(text: str) -> str:
    for pattern, replacement in NUMBERS:
        text = pattern.sub(replacement, text)
    return text


class Recorder:
    """Wraps a scripted adapter and keeps each answer, in order."""

    def __init__(self, adapter):
        self.adapter, self.answers = adapter, []

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        result = await self.adapter.complete(model, messages, schema, schema_name, effort, max_tokens, timeout_s, images)
        self.answers.append({"schema": schema_name, "text": result.text, "truncated": bool(result.truncated)})
        return result


class Player:
    """Answers with the recorded answers, in order; a request for another schema than recorded is a divergence."""

    def __init__(self, answers: list[dict]):
        self.answers, self.position, self.divergence = list(answers), 0, ""

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        if self.position >= len(self.answers):
            self.divergence = self.divergence or f"call {self.position + 1} ({schema_name}) was not recorded"
            raise RuntimeError(self.divergence)
        answer = self.answers[self.position]
        if answer["schema"] != schema_name:
            self.divergence = self.divergence or (f"call {self.position + 1} asked for {schema_name}, recorded "
                                                  f"{answer['schema']}")
        self.position += 1
        return AdapterResult(answer["text"], 10, 5, truncated=answer.get("truncated", False))


def trace(club, task_id: str) -> dict:
    """What anyone could observe of a task (normalized)."""
    task = club.task(task_id)
    said = [[item.get("speaker") or "", normalize(item["text"])] for item in club.board.messages(task_id)
            if item.get("kind") == "text"]
    steps = [[step["id"], step["kind"], step["status"]] for step in club.board.steps(task_id)]
    approvals = [item["data"]["capability"] for item in club.board.messages(task_id)
                 if item.get("code") == "approval_request"]
    outcomes = [json.loads(line)["event"] for line in club.board.outcomes.read_text().splitlines()] \
        if club.board.outcomes.is_file() else []
    commits = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=club.sample, capture_output=True,
                             text=True).stdout.strip()
    files = sorted(item["path"] for item in ((task["data"].get("change") or {}).get("files") or ()))
    return {"status": task["status"], "workflow": task["workflow"], "steps": steps, "said": said,
            "approvals": approvals, "outcomes": outcomes, "commits": int(commits or 0), "files": files}


async def run(club_factory, scenario: dict, adapter) -> tuple[dict, str]:
    """Play a scenario's inputs against a lab built by `club_factory(adapter, **config)`; return the trace."""
    club = await club_factory(adapter, **scenario.get("config", {}))
    task = None
    for step in scenario["inputs"]:
        if "new" in step:
            task = await club.new(step["new"], workflow=step.get("workflow"))
        elif "say" in step:
            await club.say(task, step["say"])
        elif "check" in step:
            await club.say(task, f"$check {task} {step['check']}")
        elif "approve" in step or "deny" in step:
            capability = step.get("approve") or step.get("deny")
            approval = [item for item in club.board.messages(task) if item.get("code") == "approval_request"
                        and item["data"]["capability"] == capability][-1]["data"]["approval_id"]
            await club.say(task, f"${'approve' if 'approve' in step else 'deny'} {approval}")
    return trace(club, task), getattr(adapter, "divergence", "")


def _waiting_change(kind, system, messages, turns):
    """A change (MAIN.md); a question about it is answered without touching anything."""
    from tests.test_workbench import route, turn
    if kind == "route":
        return route(kind="question") if "Why" in messages[-1]["content"] else route()
    if kind == "review":
        return {"verdict": "approve", "say": "", "summary": "Fine.", "issues": []}
    if "asked a question" in system:
        return turn(status="done", summary="Because it is the main one.")
    return turn(("write_file", {"path": "MAIN.md", "content": "x\n"})) if turns == 0 else turn(status="done",
                                                                                              summary="MAIN.md")


def _broken_repair(kind, system, messages, turns):
    """greet() says hi; the tester finds it; the repair breaks the project's tests, so it is undone."""
    from tests.test_workbench import criteria, route, turn
    if kind == "route":
        return route(size="small")
    if kind == "requirements":
        return criteria()
    if "the club's tester" in system:
        return {**turn(status="done", summary="Probed."),
                "findings": [{"criterion": "R1", "problem": "greet() says hi", "evidence": "printed hi"}]}
    if "tester tried to break" in system:
        return turn(("write_file", {"path": "clubnotes/schedule.py", "content": "broken(\n"})) if turns == 0 \
            else turn(status="done", summary="Fixed it, I think.")
    return turn(("write_file", {"path": "clubnotes/greet.py", "content": "def greet():\n    return 'hi'\n"})) \
        if turns == 0 else turn(status="done", summary="greet() added.")


def scenarios() -> dict:
    """The scripted scenarios the files are recorded from: name → (config, inputs, adapter factory)."""
    from extensions.workbench.demo import DemoAdapter
    from tests.test_workbench import GREET, TEAM, VERIFY, Scripted
    sunday = "Meetings on Sunday are listed first instead of last. Fix it and add a test."
    verify = {"verify": {"requirements": True, "probe": True, "repairs": 1}}
    return {
        "a_question_keeps_the_waiting_change": (TEAM, [{"new": "Add MAIN.md."}, {"say": "Why is it called MAIN?"},
                                                       {"check": "all"}, {"approve": "workbench.review"},
                                                       {"approve": "workbench.land"}],
                                                lambda: Scripted(_waiting_change)),
        "a_repair_that_breaks_the_tests_is_undone": (VERIFY, [{"new": GREET, "workflow": "single"}],
                                                     lambda: Scripted(_broken_repair)),
        "cascade_escalates_and_lands": ({}, [{"new": sunday}, {"check": "all"}, {"approve": "workbench.review"},
                                             {"approve": "workbench.land"}], DemoAdapter),
        "second_check_is_enforced": ({}, [{"new": sunday}, {"approve": "workbench.review"}, {"check": "1 2"},
                                          {"check": "all"}, {"approve": "workbench.review"}], DemoAdapter),
        "verified_with_criteria_and_a_tester": (verify, [{"new": sunday}], DemoAdapter),
        "a_question_is_answered_read_only": ({}, [{"new": "What does next_meeting do with an empty schedule?"}],
                                             DemoAdapter),
    }


async def record_all() -> None:
    import tempfile
    from tests.test_workbench import Club
    HERE.mkdir(exist_ok=True)
    for name, (config, inputs, adapter_factory) in scenarios().items():
        with tempfile.TemporaryDirectory() as folder:
            recorder = Recorder(adapter_factory())

            async def factory(adapter, **settings):
                return await Club(Path(folder), adapter, **settings).start()
            clubs = []

            async def tracked(adapter, **settings):
                club = await factory(adapter, **settings)
                clubs.append(club)
                return club
            expect, _ = await run(tracked, {"config": config, "inputs": inputs}, recorder)
            for club in clubs:
                await club.stop()
        scenario = {"name": name.replace("_", " "), "config": config, "inputs": inputs,
                    "answers": recorder.answers, "expect": expect}
        (HERE / f"{name}.json").write_text(json.dumps(scenario, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"recorded {name}: {len(recorder.answers)} answers, {len(expect['steps'])} steps")


if __name__ == "__main__":
    if sys.argv[1:2] == ["record"]:
        import asyncio
        asyncio.run(record_all())
    else:
        print(__doc__)

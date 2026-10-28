"""A scripted club for `--demo` and the tests: no keys, no model calls, the whole workflow.

It knows one task, the practice project's "Sunday is listed first" bug, and plays it as a cascade that has to
escalate: DeepSeek's quick fix breaks her own new test, the tests fail, Claude takes over and fixes it properly, and
the change set waits for your two approvals. Questions get a short answer from reading the code. Anything else is
politely refused, since a script cannot really do work.
"""

from __future__ import annotations

import json
import re

from contracts import AdapterResult

BUGGY = "        return ((DAYS.index(self.day) + 1) % 8, self.time)"
ORIGINAL = "        return ((DAYS.index(self.day) + 1) % 7, self.time)"
FIXED = "        return (DAYS.index(self.day), self.time)"
TEST = '''import unittest

from clubnotes.schedule import parse, sort_week


class SundayTests(unittest.TestCase):
    def test_sunday_comes_last(self):
        meetings = parse("Sun 10:00 Hiking\\nMon 15:30 Chess\\nWed 16:00 Robotics")
        self.assertEqual([m.day for m in sort_week(meetings)], ["Mon", "Wed", "Sun"])
'''
# DeepSeek's draft of the test also checks that Monday sorts as 0, which her own key does not give.
BUGGY_TEST = TEST + '''
    def test_monday_is_first(self):
        self.assertEqual(parse("Mon 09:00 A")[0].key()[0], 0)
'''


def _turns(messages) -> int:
    return sum(1 for message in messages if "Results of your actions" in message.get("content", ""))


def _request(messages) -> str:
    for message in messages:
        if message["role"] == "user":
            match = re.search(r"(?:Task|Question|New request) from [^:\n]+:\n(.+?)(?:\n\n|$)", message["content"], re.S)
            if match:
                return match.group(1)
    return messages[-1]["content"] if messages else ""


class DemoAdapter:
    scripted = True

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        text = "\n".join(message["content"] for message in messages)
        answer = {"chimera_route": self.route, "chimera_turn": self.turn, "chimera_review": self.review,
                  "chimera_requirements": self.requirements}.get(schema_name, self.chat)(system, messages, text)
        body = answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        return AdapterResult(body, len(text) // 4, len(body) // 4)

    def route(self, system, messages, text):
        request = messages[-1]["content"].split("Request:\n", 1)[-1]
        question = request.strip().endswith(("?", "？")) or request.lower().startswith(("what", "why", "how"))
        return {"kind": "question" if question else "change", "size": "small", "risk": "low",
                "title": " ".join(request.split()[:7])[:60], "reason": "the demo sorts every change as small"}

    def requirements(self, system, messages, text):
        if "sunday" not in text.lower() and "周日" not in text:
            return {"requirements": []}
        return {"requirements": [{"text": "Sunday meetings sort last (the week runs Monday to Sunday)", "kind": "behavior"},
                                 {"text": "A test covers where Sunday sorts", "kind": "tests"},
                                 {"text": "Meetings on the other days keep their order", "kind": "keep"}]}

    def turn(self, system, messages, text):
        n = _turns(messages)
        request = _request(messages)
        step = {"say": "", "note": "", "plan": [], "actions": [], "status": "continue", "summary": ""}
        if "the club's tester" in system:  # trying to break it: the same week on the old code and the new
            if n == 0 and "sunday" in text.lower():
                return {**step, "note": "Probing a week with Sunday and the other days, old code against new.",
                        "actions": [{"tool": "probe_file", "arguments": {
                            "path": "week.txt", "content": "Sun 10:00 Hiking\nSat 09:00 Swim\nMon 15:30 Chess\n"}},
                                    {"tool": "probe", "arguments": {"command": "python3 -m clubnotes list .probe/week.txt"}}]}
            return {**step, "status": "done", "findings": [],
                    "summary": "Sunday moved from first to last; Monday and Saturday kept their order. Nothing broke."}
        if "You can only look" in system:  # a question
            if n == 0:
                return {**step, "note": "Reading the schedule code.", "actions": [
                    {"tool": "read_file", "arguments": {"path": "clubnotes/schedule.py"}}]}
            return {**step, "status": "done", "summary": (
                "`next_meeting` calls `min()` over the meetings (clubnotes/schedule.py:47), so with an empty "
                "schedule it raises `ValueError: min() arg is an empty sequence`. Nothing checks for that today; "
                "returning `None` or raising a clearer error would be the fix.")}
        if "sunday" not in request.lower() and "周日" not in request:
            return {**step, "status": "blocked", "say": "I'm only the demo club, so I can't really do that one.",
                    "summary": "The demo only knows the practice project's Sunday task. Add a model key (see the "
                               "README) and the real classmates can do anything."}
        if "taking over" in system:  # Claude, after the draft failed
            if n == 0:
                return {**step, "say": "Let me see what went wrong.", "note": "Reading the key and the new test.",
                        "plan": [{"step": "Find why the tests fail", "done": False},
                                 {"step": "Make Monday 0 and Sunday 6", "done": False}],
                        "actions": [{"tool": "read_file", "arguments": {"path": "clubnotes/schedule.py"}},
                                    {"tool": "read_file", "arguments": {"path": "tests/test_sunday.py"}}]}
            if n == 1:
                return {**step, "note": "Monday should be 0 and Sunday 6: index into DAYS directly.",
                        "plan": [{"step": "Find why the tests fail", "done": True},
                                 {"step": "Make Monday 0 and Sunday 6", "done": False}],
                        "actions": [{"tool": "edit_file", "arguments": {"path": "clubnotes/schedule.py",
                                                                         "old": BUGGY, "new": FIXED}},
                                    {"tool": "edit_file", "arguments": {
                                        "path": "clubnotes/schedule.py",
                                        "old": "        # Python's isoweekday() counts Monday as 1 and Sunday as 7; "
                                               "this uses % 7, so Sunday sorts as day 0.\n",
                                        "new": "        # Monday is 0 and Sunday 6, the order of DAYS.\n"}},
                                    {"tool": "run", "arguments": {"command": "python3 -m unittest discover -s tests -t ."}}]}
            return {**step, "status": "done", "plan": [{"step": "Find why the tests fail", "done": True},
                                                       {"step": "Make Monday 0 and Sunday 6", "done": True}],
                    "summary": "Sunday now sorts last: `Meeting.key` uses the day's position in `DAYS` (Monday 0 "
                               "... Sunday 6) instead of the `% 7` shift that put Sunday first. DeepSeek's draft "
                               "used `% 8`, which moved Sunday but made Monday 1; her new tests caught it. All tests "
                               "pass now."}
        if n == 0:  # DeepSeek's quick draft
            return {**step, "say": "Leave it to me~ looking at how the week is sorted.", "note": "Finding the sort key.",
                    "plan": [{"step": "Find the sort key", "done": False}, {"step": "Fix Sunday", "done": False},
                             {"step": "Add a test", "done": False}],
                    "actions": [{"tool": "search", "arguments": {"pattern": "def key"}},
                                {"tool": "read_file", "arguments": {"path": "clubnotes/schedule.py"}}]}
        if n == 1:
            return {**step, "note": "Shifting the modulus so Sunday is no longer 0, and adding tests.",
                    "plan": [{"step": "Find the sort key", "done": True}, {"step": "Fix Sunday", "done": False},
                             {"step": "Add a test", "done": False}],
                    "actions": [{"tool": "edit_file", "arguments": {"path": "clubnotes/schedule.py", "old": ORIGINAL,
                                                                     "new": BUGGY}},
                                {"tool": "write_file", "arguments": {"path": "tests/test_sunday.py",
                                                                      "content": BUGGY_TEST}},
                                {"tool": "run", "arguments": {"command": "python3 -m unittest discover -s tests -t ."}}]}
        return {**step, "status": "done", "say": "Done... mostly. One test is being difficult.",
                "plan": [{"step": "Find the sort key", "done": True}, {"step": "Fix Sunday", "done": True},
                         {"step": "Add a test", "done": True}],
                "summary": "Changed the sort key so Sunday is no longer day 0, and added tests for Sunday. One test "
                           "still fails; I think it expects too much."}

    def review(self, system, messages, text):
        if "quick check" in system:  # the judge
            return {"verdict": "approve", "say": "Looks right to me, nya.", "summary": "The change does the task.",
                    "issues": []}
        return {"verdict": "approve", "say": "Fine work. One small note, nothing blocking.",
                "summary": "Correct and tested.", "issues": [
                    {"severity": "minor", "file": "clubnotes/schedule.py", "line": 18,
                     "problem": "The comment could say why the order matters.", "suggestion": "Mention sort_week."}]}

    def chat(self, system, messages, text):
        return "Hello! I'm the demo club. Only the creator can start real work here."

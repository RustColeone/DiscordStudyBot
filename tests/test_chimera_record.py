"""Each student's record in the Classroom (extensions/chimera/record.py): the sessions she worked in, her level and
art stage from the ones you land, and her measured benchmark runs, published as facts for the pages."""

import importlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.core_support import ROOT


def chimera(name):
    """A module of the chimera extension, imported the way the core does (as a private package)."""
    import sys
    from core.loader import import_extension
    _, module = import_extension(ROOT / "extensions" / "chimera")
    return importlib.import_module(f"{module.__name__.rsplit('.', 1)[0]}.{name}")


record = chimera("record")
Board = chimera("board").Board
verify = chimera("kernel.verify")


class FakeFacts:
    def __init__(self):
        self.values = {}

    def set(self, persona, key, value, *, person=None):
        if value is None:
            self.values.pop((persona, key), None)
        else:
            self.values[(persona, key)] = value


class RecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.board = Board(Path(self._tmp.name) / "board.db")

    def tearDown(self):
        self.board.close()
        self._tmp.cleanup()

    def session(self, task_id, status, *seats):
        self.board.create_task(task_id, f"classroom:{task_id}", "practice", task_id)
        for number, seat in enumerate(seats):
            self.board.put_step(task_id, f"s{number}", seat=seat, kind="model", status="done")
        self.board.update_task(task_id, status=status)

    def test_a_level_for_every_three_landed_sessions_and_an_art_stage_at_ten_twenty_thirty(self):
        self.assertEqual(record.level_of(0), {"level": 1, "toward": 0, "per_level": 3, "stage": 1, "stages": 4,
                                              "next_stage_at": 10})
        self.assertEqual((record.level_of(5)["level"], record.level_of(5)["toward"]), (2, 2))
        self.assertEqual((record.level_of(27)["level"], record.level_of(27)["stage"]), (10, 2))
        self.assertEqual(record.level_of(87)["stage"], 4)
        self.assertIsNone(record.level_of(87)["next_stage_at"])  # every stage open

    def test_a_student_is_counted_in_the_sessions_she_worked_in_by_how_they_ended(self):
        self.session("t1", "landed", "gpt", "deepseek", "deepseek")  # her two steps count once
        self.session("t2", "landed", "deepseek")
        self.session("t3", "discarded", "deepseek")
        self.session("t4", "review", "gpt")
        self.session("t5", "failed", "gpt")
        found = record.records(self.board)
        self.assertEqual({key: found["deepseek"][key] for key in ("landed", "discarded", "open")},
                         {"landed": 2, "discarded": 1, "open": 0})
        self.assertEqual({key: found["gpt"][key] for key in ("landed", "discarded", "open")},
                         {"landed": 1, "discarded": 0, "open": 1})
        self.assertEqual(found["deepseek"]["toward"], 2)

    def test_her_benchmark_rows_are_the_measured_teams_she_was_in(self):
        summaries = record.benchmarks()  # the polyglot round, as exported into benchmarks/
        rows = record.benchmark_rows("deepseek", summaries)
        alone = next(row for row in rows if row["setup"] == "flash-native-notester")
        self.assertEqual((alone["seats"], alone["points"], alone["total"], alone["solved"], alone["exercises"]),
                         (["builder"], 558, 584, 23, 34))
        self.assertAlmostEqual(alone["session_offpeak"], 0.4229 / 34, places=4)
        planned = next(row for row in rows if row["setup"] == "plan")
        self.assertEqual((planned["seats"], planned["share"], planned["team"]["planner"]), (["builder"], 0.34, "gpt"))
        self.assertEqual({row["setup"] for row in record.benchmark_rows("gpt", summaries)}, {"sol", "plan", "plan-tester"})
        self.assertEqual(record.benchmark_rows("claude", summaries), [])  # never measured

    def test_the_facts_follow_the_board(self):
        facts = FakeFacts()
        team = SimpleNamespace(seats={"gpt": None, "flash": None})  # a seat of the team's own, and no DeepSeek seat
        records = record.Records(self.board, team, facts)
        records.publish()
        self.assertEqual(facts.values[("gpt", "record")]["level"], 1)
        self.assertEqual(facts.values[("flash", "record")]["landed"], 0)
        self.assertTrue(facts.values[("deepseek", "benchmark")])  # measured, though not seated here
        self.assertNotIn(("flash", "benchmark"), facts.values)
        self.board.subscribe(records.listen)
        for number in range(3):
            self.session(f"t{number}", "landed", "gpt")
        self.assertEqual((facts.values[("gpt", "record")]["level"], facts.values[("gpt", "record")]["landed"]), (2, 3))


class TesterSwitchTests(unittest.TestCase):
    def test_the_tester_seat_switch_of_a_picked_team_wins_over_the_setting(self):
        outcome = SimpleNamespace(kind="change", ok=True, change={"files": [{"path": "a.py"}]})
        tiny = SimpleNamespace(signals={"size": "tiny"}, requirements=[])
        def ctx(team, probe="auto"):
            return SimpleNamespace(picked=lambda: team, settings={"verify": {"probe": probe}})
        self.assertFalse(verify.wanted(ctx({}), tiny, outcome))  # auto: a tiny change goes untested
        self.assertTrue(verify.wanted(ctx({"tester": True}), tiny, outcome))  # switched on for the session
        self.assertFalse(verify.wanted(ctx({"tester": False}, probe=True), tiny, outcome))  # switched off
        nothing = SimpleNamespace(kind="change", ok=True, change={"files": []})
        self.assertFalse(verify.wanted(ctx({"tester": True}), tiny, nothing))  # nothing changed: nothing to test


if __name__ == "__main__":
    unittest.main()

"""Each student's record in the Classroom, published as facts for the pages (contracts 1.9).

    the board's sessions ──► per student: those she worked in (a step in her seat), by how they ended
                             (landed, discarded, open) ──► `chimera.record` {landed, discarded, open, level, ...}
    benchmarks/*.json    ──► per student: the measured teams she was in ──► `chimera.benchmark` [rows]

Her level comes from the work you land: Lv = 1 + landed // PER_LEVEL, and each art stage opens at a level in STAGES.
Work you discard gives nothing. The pages (the Students page, her card) read the facts; nothing else does.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

PER_LEVEL = 3  # landed sessions per level
STAGES = (1, 10, 20, 30)  # the level each art stage opens at
BENCHMARKS = Path(__file__).resolve().parent / "benchmarks"


def level_of(landed: int) -> dict:
    """Her level, how far she is towards the next, and her art stage, from the sessions landed with her."""
    level = 1 + landed // PER_LEVEL
    stage = sum(level >= at for at in STAGES)
    return {"level": level, "toward": landed % PER_LEVEL, "per_level": PER_LEVEL, "stage": stage,
            "stages": len(STAGES), "next_stage_at": STAGES[stage] if stage < len(STAGES) else None}


def records(board) -> dict[str, dict]:
    """Every student's sessions by how they ended, with her level."""
    counts: dict[str, Counter] = defaultdict(Counter)
    for task in board.tasks(5000):
        bucket = task["status"] if task["status"] in ("landed", "discarded") else (
            "open" if task["status"] in ("queued", "working", "review", "approved") else "other")
        # (the router only sorts the request: routing is not working on the session)
        for seat in {step["seat"] for step in board.steps(task["id"]) if step["seat"] and step["role"] != "router"}:
            counts[seat][bucket] += 1
    return {seat: {"landed": found["landed"], "discarded": found["discarded"], "open": found["open"],
                   **level_of(found["landed"])} for seat, found in counts.items()}


def benchmarks(folder: Path = BENCHMARKS) -> list[dict]:
    found = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        try:
            summary = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(summary, dict) and isinstance(summary.get("teams"), list):
            found.append(summary)
    return found


def benchmark_rows(persona: str, summaries: list[dict]) -> list[dict]:
    """The measured teams she was in: her seats in each, her model, and the team's results per exercise."""
    rows = []
    for summary in summaries:
        for team in summary["teams"]:
            seats = [role for role, holder in (team.get("seats") or {}).items() if holder == persona]
            if not seats:
                continue
            exercises = max(1, int(team.get("exercises") or summary.get("exercises") or 1))
            rows.append({"benchmark": summary.get("name", ""), "date": summary.get("date", ""),
                         "setup": team.get("setup", ""), "formation": team.get("formation", ""), "seats": seats,
                         "team": team.get("seats") or {}, "model": (team.get("models") or {}).get(persona, ""),
                         "points": team.get("points"), "total": team.get("total"), "solved": team.get("solved"),
                         "exercises": exercises, "session_offpeak": round(float(team.get("offpeak") or 0) / exercises, 4),
                         "session_peak": round(float(team.get("peak") or 0) / exercises, 4),
                         "median_min": team.get("median_min"), "share": (team.get("share") or {}).get(persona)})
    return rows


class Records:
    """Keeps the facts in step with the board: published at start, and again when a session lands or is discarded."""

    def __init__(self, board, team, facts, folder: Path = BENCHMARKS):
        self.board, self.team, self.facts = board, team, facts
        self.summaries = benchmarks(folder)

    def publish(self) -> None:
        if self.facts is None:
            return
        found = records(self.board)
        # every seat, everyone who ever worked here, and every student in a measured team (a student the team does
        # not seat still has her benchmark runs)
        measured = {holder for summary in self.summaries for team in summary["teams"]
                    for holder in (team.get("seats") or {}).values()}
        for persona in sorted({*self.team.seats, *found, *measured}):
            self.facts.set(persona, "record", found.get(persona) or {"landed": 0, "discarded": 0, "open": 0,
                                                                        **level_of(0)})
            self.facts.set(persona, "benchmark", benchmark_rows(persona, self.summaries) or None)

    def listen(self, event: str, payload: dict) -> None:
        if event == "task" and payload.get("status") in ("landed", "discarded"):
            self.publish()

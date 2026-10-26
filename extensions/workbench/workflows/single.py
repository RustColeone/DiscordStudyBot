"""Single: one classmate does it (and a question is answered read-only, before any workflow; see kernel/flow.py)."""

from __future__ import annotations

from . import workflow


@workflow("single", "Single", "One classmate does it (questions are read-only)")
async def single(round):
    await round.say("route.say", round.say_route(drafter=round.first("drafter")))
    mode = "followup" if round.followup else "single"
    solved = await round.solve(f"{round.r}.single", "drafter", mode, round.base_brief, turns="single")
    validation = await round.validate("single.test")
    change = await round.diff("single.diff")
    return round.outcome(solved, change, validation)

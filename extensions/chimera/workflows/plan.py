"""Plan: a strong classmate studies the task and writes the plan; a cheaper one carries it out.

The planner only reads (list_files, search, read_file) and answers with the plan: what to change and where, with
exact signatures; the behaviour asked for, down to its edge cases and exact messages; and how to check each part.
The drafter then works from the task and the plan, writes the code and runs the tests. Like every workflow, the
change is verified afterwards (the criteria, the tester's probes when they are on, one repair). A planner who
cannot finish a plan leaves the drafter the task alone, as in `single`.
"""

from __future__ import annotations

from . import workflow


@workflow("plan", "Plan", "A strong classmate plans it; another carries out the plan")
async def plan(round):
    planner, drafter = round.first("planner"), round.first("drafter")
    await round.say("route.say", round.say_route(planner=planner, drafter=drafter))
    planned = await round.solve(f"{round.r}.plan", "planner", "plan", round.base_brief, write=False, turns="plan",
                                tier="strong")
    brief = round.base_brief
    if planned.status == "done" and planned.summary.strip():
        brief += f"\n\nThe plan from {planned.seat.name}:\n{planned.summary.strip()}"
    solved = await round.solve(f"{round.r}.execute", "drafter", "execute", brief, turns="execute")
    validation = await round.validate("plan.test")
    change = await round.diff("plan.diff")
    return round.outcome(solved, change, validation)

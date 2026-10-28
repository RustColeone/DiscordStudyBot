"""Critique: a draft, a review by another classmate (another model family where there is one), one revision."""

from __future__ import annotations

from . import workflow
from ..kernel.flow import Stopped, tests_text


@workflow("critique", "Critique",
          "A draft, a review by another classmate (another model family where there is one), one revision")
async def critique(round):
    r, ctx = round.r, round.ctx
    heavy = round.signals.get("size") == "large" or round.signals.get("risk") == "high"
    role = "strong" if heavy else "drafter"
    drafter = round.first(role)
    critic = round.first("critic", drafter)
    await round.say("route.say", round.say_route(drafter=drafter, critic=critic))
    mode = "followup" if round.followup else "draft"
    drafted = await round.solve(f"{r}.draft", role, mode, round.base_brief, turns=mode,
                                tier="strong" if heavy else "cheap")
    validation = await round.validate("draft.test")
    change = await round.diff("draft.diff")
    if not change.get("files"):
        return round.outcome(drafted, change, validation)
    ctx.update(phase="under review")
    try:
        verdict = await round.review_change("critic", drafted, change, validation)
    except Stopped as error:  # nobody could review it: it comes to you as it is, marked unreviewed
        return round.outcome(drafted, change, validation, warning=round.unreviewed(error))
    ctx.update(data={"review": verdict.to_dict()})
    if verdict.approve and validation.get("ok") is not False and drafted.status == "done":
        return round.outcome(drafted, change, validation, verdict)
    await round.say("revise.say", round.told("revise", me=verdict.seat, mine=drafted.seat, drafter=drafted.seat.name,
                                             critic=verdict.seat.name if verdict.seat else ""))
    brief = (f"{round.base_brief}\n\n## Your change so far\n{drafted.summary}\n\nChange set: {change.get('stat')}\n\n"
             f"## The review (by {verdict.seat.name if verdict.seat else 'a classmate'})\n{verdict.brief()}\n\n"
             f"{tests_text(validation)}")
    revised = await round.solve(f"{r}.revise", role, "revise", brief, seat=drafted.seat,
                                tier="strong" if heavy else "cheap")
    validation = await round.validate("revise.test")
    change = await round.diff("revise.diff")
    return round.outcome(revised, change, validation, verdict, drafter=drafted.seat)

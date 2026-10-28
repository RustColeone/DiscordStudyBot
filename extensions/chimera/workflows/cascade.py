"""Cascade: a cheap draft, the tests and a quick judge; a stronger classmate takes over only if it does not pass."""

from __future__ import annotations

from . import workflow
from ..kernel.flow import Stopped, tests_text


@workflow("cascade", "Cascade", "A cheap draft, tests and a quick check; a stronger classmate takes over only if needed")
async def cascade(round):
    r = round.r
    drafter = round.first("drafter")
    judge, strong = round.first("judge", drafter), round.first("strong", drafter)
    await round.say("route.say", round.say_route(drafter=drafter, judge=judge, strong=strong))
    mode = "followup" if round.followup else "draft"
    drafted = await round.solve(f"{r}.draft", "drafter", mode, round.base_brief, turns=mode)
    validation = await round.validate("draft.test")
    change = await round.diff("draft.diff")
    if drafted.status == "done" and not change.get("files"):
        return round.outcome(drafted, change, validation)
    verdict = None
    if drafted.status != "done":
        reason = f"the draft is unfinished ({drafted.status})"
    elif validation.get("ok") is False:
        reason = f"the tests fail ({validation.get('summary')})"
    else:
        round.ctx.update(phase="checking")
        try:
            verdict = await round.review_change("judge", drafted, change, validation)
        except Stopped as error:  # nobody could check it: the tests passed, so it comes to you, unreviewed
            return round.outcome(drafted, change, validation, warning=round.unreviewed(error))
        if verdict.approve:
            return round.outcome(drafted, change, validation, verdict)
        reason = verdict.summary or "the judge found problems"
    await round.record("escalated", reason=reason)
    takeover = round.first("strong", drafted.seat)
    await round.say("escalate.say", round.told("escalate", me=takeover, mine=drafted.seat, strong=takeover.name,
                                                drafter=drafted.seat.name, reason=reason))
    brief = (f"{round.base_brief}\n\n## The draft so far (by {drafted.seat.name})\n{drafted.summary}\n\n"
             f"Change set: {change.get('stat')}\n```diff\n{str(change.get('patch', ''))[:30000]}\n```\n\n"
             f"## Why it did not pass\n{reason}\n\n{tests_text(validation)}"
             + (f"\n\n## The check\n{verdict.brief()}" if verdict else ""))
    escalated = await round.solve(f"{r}.escalate", "strong", "escalate", brief, avoid=round.family(drafted),
                                  not_seat=drafted.seat.persona)
    validation = await round.validate("escalate.test")
    change = await round.diff("escalate.diff")
    return round.outcome(escalated, change, validation, verdict, drafter=drafted.seat)

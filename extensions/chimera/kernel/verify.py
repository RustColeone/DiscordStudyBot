"""Verification: what the club checks on any change before it comes to you, whatever workflow made it.

1. **Acceptance criteria.** At the start of a round, the request becomes a short list of checkable criteria
   (behaviour, "keep X unchanged", tests, docs), kept with the task and extended by follow-ups. The drafter reads
   them, reviewers say per criterion whether it is met and point at changes no criterion explains.
2. **The tester.** Once a workflow has a change whose tests pass, a classmate (role `tester`: from another company
   than the one who wrote it where one can answer, else the tester line-up's first, cheapest first) tries to break it: edge cases for each behaviour, and for each "keep" criterion the
   same input on the new code and on the base commit (`probe`), so "unchanged" is checked by running it, not by
   reading the diff. What she shows with a probe is a finding.
3. **Repair.** Findings go back to whoever finished the change for one more pass (`repairs`, 1), then the tester
   looks again. Whatever is still found comes to you as a warning and a checklist item, never silently.

Settings (config.yml `verify:`): `requirements` (true), `probe` (auto: every change but a tiny one, or any change
with a "keep" criterion; true; false), `repairs` (1), `independent_tester` (true: another company than the author
tests where one can; false: the tester line-up's order alone, cheapest first), turns for the tester under
`turns: {probe: 10}`.
"""

from __future__ import annotations

import re

from contracts import ModelError

from . import prompts


def settings(ctx) -> dict:
    return {"requirements": True, "probe": "auto", "repairs": 1, "independent_tester": True,
            **dict(ctx.settings.get("verify") or {})}


async def requirements(ctx, round) -> list[dict]:
    """The task's acceptance criteria: those of earlier rounds, plus what this round's request adds."""
    earlier = list((ctx.task().get("data") or {}).get("requirements") or ())
    if not settings(ctx)["requirements"]:
        return earlier
    key = f"{round.r}.requirements"

    async def reading(seat, tier):
        answer = await ctx.model(f"{key}.m", seat, "cheap", [
            {"role": "system", "content": prompts.REQUIREMENTS},
            {"role": "user", "content": (f"The task already has these criteria:\n"
                                         + "\n".join(f"- {item['text']}" for item in earlier)
                                         + "\n\nA follow-up request on it. List only criteria it adds or changes, "
                                           "never one already above (in other words); usually none (\"try again\", "
                                           "\"fix it\" add nothing):\n"
                                         if earlier else "Task:\n") + round.text}],
            schema=prompts.REQUIREMENTS_SCHEMA, schema_name="chimera_requirements", effort="low", max_tokens=1500,
            timeout_s=90, step=key, role="router")
        return answer["data"].get("requirements") or []
    from .flow import Stopped  # (flow imports this module)
    try:
        found = await round.staffed(key, "router", reading, quiet=True)
    except (Stopped, ModelError):  # nobody could read it: the round goes on without new criteria (reviews still look)
        return earlier
    seen = {item["text"].strip().lower() for item in earlier}
    added = []
    for item in found[:5 if earlier else 10]:  # a follow-up adds a few at most
        text = str(item.get("text") or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            added.append({"id": f"R{len(earlier) + len(added) + 1}", "text": text[:300],
                          "kind": item.get("kind") if item.get("kind") in ("behavior", "keep", "tests", "docs")
                          else "other", "round": round.number})
    criteria = earlier + added
    ctx.update(data={"requirements": criteria})
    ctx.step(key, kind="requirements", role="router", status="done", round=round.number,
             title=f"Acceptance criteria: {len(criteria)}", data={"requirements": criteria, "added": len(added)})
    return criteria


def section(criteria: list[dict]) -> str:
    """The criteria as the drafter reads them in her brief."""
    if not criteria:
        return ""
    return ("Acceptance criteria (each is checked by a reviewer and a tester before the change comes to the person; "
            "do what they say and nothing they don't ask for):\n"
            + "\n".join(f"- {item['id']} ({item['kind']}): {item['text']}" for item in criteria))


def tester_for(round, outcome):
    """Who tests a change: a classmate from another company than the one who finished it, when one can answer (an
    independent reading); within one company, the tester line-up's order decides (cheapest first): two models from
    one company are hardly more independent of each other, and testing is the costliest step of a small change.
    With `verify: {independent_tester: false}` the line-up's order alone decides (the cheapest always tests)."""
    return round.lineup("tester", tester_avoid(round, outcome))[0]


def tester_avoid(round, outcome):
    independent = settings(round.ctx)["independent_tester"] not in (False, "false", "no", "off")
    return outcome.seat.family if independent and outcome.seat else None


def wanted(ctx, round, outcome) -> bool:
    if outcome.kind != "change" or not outcome.ok or not outcome.change.get("files"):
        return False
    tester = ctx.picked().get("tester")
    if isinstance(tester, bool):  # the tester seat, switched on or off for this session on the page
        return tester
    probe = settings(ctx)["probe"]
    if probe in (False, "false", "off", "no"):
        return False
    if probe in (True, "true", "on", "yes"):
        return True
    return round.signals.get("size") != "tiny" or any(item["kind"] == "keep" for item in round.requirements)


async def test(round, outcome, attempt: int):
    """The tester tries to break the change. Returns (findings, how many probes she ran, who she was)."""
    ctx, r = round.ctx, round.r
    key = f"{r}.probe" + (f".{attempt}" if attempt else "")
    criteria = section(round.requirements) or f"(No criteria were written down; use the task.)"
    brief = (f"Task from {ctx.person}:\n{round.text}\n\n{criteria}\n\n{round.rules}\n\nThe change "
             f"({outcome.change.get('stat')}), by {outcome.seat.name if outcome.seat else 'a classmate'}:\n"
             f"```diff\n{str(outcome.change.get('patch', ''))[:30000]}\n```\n\nTests: "
             f"{outcome.validation.get('summary') or 'none ran'}")
    solved = await round.solve(key, "tester", "probe", brief, write=False, turns="probe",
                               avoid=tester_avoid(round, outcome))  # (as `tester_for` names her)
    turn = re.compile(re.escape(key) + r"\.\d+")  # her turns only (`r1.probe.3`, never the next look's `r1.probe.1.3`)
    probes = sum(1 for step in ctx.board.steps(ctx.task_id) if turn.fullmatch(step["id"])
                 for action in step["data"].get("actions") or () if action.get("tool") == "probe")
    return solved.findings, probes, solved.seat


async def verify(round, outcome):
    """Test the change; on findings, one repair by whoever finished it, then test again. The outcome carries what
    was checked (`verification`) and, if something is still wrong, a warning for you."""
    ctx, r = round.ctx, round.r
    if not wanted(ctx, round, outcome):
        return outcome
    from .flow import Stopped  # (flow imports this module)
    ctx.update(phase="testing it")
    record = {"requirements": round.requirements, "rounds": [], "repairs": 0}
    expected = tester_for(round, outcome)
    await round.say("probe.say", round.told("verify_start", me=expected, tester=expected.name))
    try:
        findings, probes, tester = await test(round, outcome, 0)
    except Stopped as error:  # nobody could test it: it still comes to you, saying so
        outcome.verification = {**record, "tester": "", "note": f"not tested: {error}"}
        return outcome
    record["rounds"].append({"findings": findings, "probes": probes, "tester": tester.persona})
    retested, reverted = True, ""
    for attempt in range(1, int(settings(ctx)["repairs"]) + 1):
        if not findings:
            break
        fixer = outcome.seat or round.first("drafter")
        problems = "; ".join(item.get("problem", "") for item in findings[:3]) + (" …" if len(findings) > 3 else "")
        await round.say(f"probe.found.{attempt}", round.told("verify_found", me=tester, mine=fixer, tester=tester.name,
                                                              fixer=fixer.name, count=len(findings), problems=problems))
        brief = (f"{round.base_brief}\n\n## Your change so far\n{outcome.summary}\n\nChange set: "
                 f"{outcome.change.get('stat')}\n\n## What the tester found\n"
                 + "\n".join(f"- {item.get('criterion')}: {item.get('problem')}\n  Evidence: {item.get('evidence')}"
                             for item in findings))
        good = outcome  # tested, passing: what comes to you if the repair makes it worse
        record["repairs"] = attempt
        try:
            repaired = await round.solve(f"{r}.repair{attempt}", "drafter", "repair", brief, seat=fixer,
                                         tier=outcome.tier, turns="repair")  # (her models: the change's own tier)
            validation = await round.validate(f"repair{attempt}.test")
            change = await round.diff(f"repair{attempt}.diff")
            outcome = round.outcome(repaired, change, validation, outcome.verdict, drafter=outcome.drafter,
                                    warning=outcome.warning)
            if good.summary and repaired.summary:  # the whole story: what was built, then what the findings changed
                outcome.summary = f"{good.summary}\n\n**After the tester's findings:** {repaired.summary}"
            failed = "" if outcome.ok else outcome.reason
        except Stopped as error:  # nobody could repair it
            failed = str(error)
        if failed:  # the repair broke it: the worktree goes back to the tested change, her findings stay
            outcome = await _revert(round, good, f"repair{attempt}") or outcome
            reverted = failed if outcome is good else ""
            break
        try:
            findings, probes, tester = await test(round, outcome, attempt)
        except Stopped:
            retested = False  # the repair stands; what the tester found before is still reported below
            break
        record["rounds"].append({"findings": findings, "probes": probes, "tester": tester.persona})
    total = sum(item["probes"] for item in record["rounds"])
    outcome.verification = {**record, "tester": tester.persona, "probes": total, "findings": findings}
    if not outcome.ok:
        return outcome
    if findings:
        problems = "; ".join(item.get("problem", "") for item in findings[:3])
        key = "repair_reverted" if reverted else "verify_left" if retested or not record["repairs"] \
            else "verify_left_untested"
        outcome.warning = " ".join(filter(None, [outcome.warning, round.told(
            key, tester=tester.name, count=len(findings), problems=problems, reason=reverted.strip().rstrip("."))]))
    else:
        await round.say("probe.clean", round.told("verify_clean", me=tester, tester=tester.name, probes=total))
    return outcome


async def _revert(round, good, phase: str):
    """The worktree back to `good`'s change set; `good` if that worked, else None (the broken state stays, failed)."""
    tree = good.change.get("tree")
    if not tree:
        return None
    restored = await round.ctx.tool(f"{round.r}.{phase}.undo", "restore", {"tree": tree})
    if restored.status != "success" or restored.data.get("fingerprint") != good.change.get("fingerprint"):
        return None
    round.ctx.step(f"{round.r}.{phase}.undo", kind="change", role="", status="done", round=round.number,
                   title=f"Back to the tested change · {restored.data.get('stat')}",
                   data={key: restored.data.get(key) for key in ("files", "stat", "fingerprint", "added", "removed",
                                                                 "tree")})
    return good

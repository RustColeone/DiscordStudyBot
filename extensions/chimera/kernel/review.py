"""Reviewers: a classmate who reads the change and says approve or revise. They get no tools and touch nothing;
they see only the task, its acceptance criteria, the diff and the test output (the critic in a critique, the judge
in a cascade). The verdict follows what they found, not the word they chose: a criterion not met, a change nobody
asked for, or a correctness, requirement or scope issue blocks, whatever its severity."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..team import Seat
from . import prompts
from .context import Context, SeatUnavailable

PATCH_CHARS = 60_000
OUTPUT_CHARS = 4_000


@dataclass
class Verdict:
    approve: bool
    summary: str
    issues: list[dict] = field(default_factory=list)
    seat: Seat | None = None
    provider: str = ""
    requirements: list[dict] = field(default_factory=list)  # [{id, met, evidence}] for the task's criteria
    unexplained: list[dict] = field(default_factory=list)   # [{file, change}] nobody asked for

    def blocking(self, evidence: bool = False) -> list[dict]:
        """The issues that send the change back: a blocker or major one, or a correctness, requirement or scope one
        of any severity; with `evidence` (setting minor_needs_failing_input), a minor one of those only when it names
        an input that fails, the rest being notes on your checklist."""
        return [issue for issue in self.issues if issue.get("severity") in ("blocker", "major")
                or issue.get("category") in prompts.BLOCKING
                and (not evidence or str(issue.get("failing_input") or "").strip())]

    def to_dict(self) -> dict:
        return {"approve": self.approve, "summary": self.summary, "issues": self.issues,
                "seat": self.seat.persona if self.seat else "", "provider": self.provider,
                "requirements": self.requirements, "unexplained": self.unexplained}

    def brief(self) -> str:
        """The review as the next solver reads it."""
        lines = [f"Verdict: {'approve' if self.approve else 'revise'}. {self.summary}".strip()]
        for issue in self.issues:
            where = issue.get("file") or "?"
            if issue.get("line"):
                where += f":{issue['line']}"
            lines.append(f"- [{issue.get('severity')}{', ' + issue['category'] if issue.get('category') else ''}] "
                         f"{where}: {issue.get('problem')} → {issue.get('suggestion')}")
        for item in self.requirements:
            if item.get("met") != "yes":
                lines.append(f"- criterion {item.get('id')} {'not met' if item.get('met') == 'no' else 'unclear'}: "
                             f"{item.get('evidence')}")
        for item in self.unexplained:
            lines.append(f"- not asked for: {item.get('file')}: {item.get('change')} (undo it unless a criterion needs it)")
        return "\n".join(lines)


def validation_text(validation: dict) -> str:
    if not validation.get("ran"):
        return "No tests were run (the project has no test command)."
    text = f"Test command: {validation.get('command')}\nResult: {validation.get('summary')}"
    if validation.get("baseline_failed"):
        text += "\n(The tests already failed before this change.)"
    output = str(validation.get("output") or "")
    return text + (f"\nOutput (end):\n{output[-OUTPUT_CHARS:]}" if output else "")


async def review(ctx: Context, seat: Seat, tier: str, phase: str, *, mode: str, task_text: str, change: dict,
                 validation: dict, role: str, rules: str = "", requirements: list[dict] | None = None) -> Verdict:
    language = prompts.language_name(ctx.language)
    system = (prompts.CRITIC if mode == "critic" else prompts.JUDGE).format(voice=seat.voice, person=ctx.person,
                                                                             language=language)
    patch = str(change.get("patch") or "")
    shown = patch[:PATCH_CHARS] + (f"\n… (the diff goes on: {len(patch) - PATCH_CHARS} more characters)"
                                   if len(patch) > PATCH_CHARS else "")
    criteria = "\n".join(f"- {item['id']} ({item['kind']}): {item['text']}" for item in requirements or ())
    user = (f"Task from {ctx.person}:\n{task_text}\n\n" + (f"{rules}\n\n" if rules else "")
            + (f"Acceptance criteria:\n{criteria}\n\n" if criteria else "")
            + f"Change set: {change.get('stat')}\n```diff\n{shown}\n```\n\n{validation_text(validation)}")
    step = phase
    title = f"{seat.name} · {'reviewing' if mode == 'critic' else 'checking the draft'}"
    if not ctx.kept(f"{step}.m"):  # a replay leaves the finished step as it was
        ctx.step(step, kind="review", role=role, seat=seat.persona, status="running", title=title, data={"mode": mode})
    await ctx.progress(f"{title}…", key=f"{step}.m")
    try:
        answer = await ctx.model(f"{step}.m", seat, tier, [{"role": "system", "content": system},
                                                            {"role": "user", "content": user}],
                                 schema=prompts.REVIEW_SCHEMA, schema_name="chimera_review",
                                 effort=str(ctx.settings.get("review_effort", "medium")),
                                 max_tokens=int(ctx.settings.get("review_tokens", 12000)),
                                 timeout_s=float(ctx.settings.get("review_timeout_s", 150)), step=step, role=role)
    except SeatUnavailable as error:  # closed, not left running: another reviewer takes the step, or nobody does
        ctx.step(step, status="failed", data={"error": error.reason})
        raise
    data = answer["data"]
    issues = [dict(issue) for issue in data.get("issues") or ()]
    known = {item["id"] for item in requirements or ()}
    met = [dict(item) for item in data.get("requirements") or () if item.get("id") in known]
    unexplained = [dict(item) for item in data.get("unexplained") or ()]
    verdict = Verdict(False, str(data.get("summary") or ""), issues, seat, answer["provider"], met, unexplained)
    # The verdict follows what was found, whatever the word says: a blocking issue (a blocker or major, or a real bug,
    # a criterion or a change nobody asked for at any severity), a criterion not met, or something unexplained.
    evidence = bool(ctx.settings.get("minor_needs_failing_input", False))
    found = (verdict.blocking(evidence) or [item for item in met if item.get("met") == "no"]
             or (unexplained if not evidence else []))  # (with evidence, unasked-for changes are checklist notes)
    verdict.approve = not found and not (data["verdict"] == "revise" and not issues)
    approve = verdict.approve
    ctx.step(step, status="done", data={"verdict": "approve" if approve else "revise", "summary": verdict.summary,
                                        "issues": issues, "requirements": met, "unexplained": unexplained,
                                        "say": data.get("say", "")})
    await ctx.say(f"{step}.say", seat, data.get("say", ""))
    return verdict

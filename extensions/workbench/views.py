"""What a task looks like from outside: its current approval with the checklist and ticks, what can be done with it
now, and the report it sends back. One place for these rules, so the page, `club`, Discord's `$commands` and any
other frontend agree (they render; they never decide).
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from .board import CLOSED

REST = ("review", "approved", "answered", "failed", "cancelled")


def checklist(board, task: dict) -> tuple[str, list[dict]]:
    """The second check of the task's current change set: (fingerprint, [{id, label, ticked}]). The engine writes it
    on the round's `check` step; ticks are kept per fingerprint, so a new version starts unticked."""
    fingerprint = ((task.get("data") or {}).get("change") or {}).get("fingerprint") or ""
    step = next((step for step in reversed(board.steps(task["id"])) if step["kind"] == "approval"
                 and step["data"].get("stage") == "review" and step["data"].get("fingerprint") == fingerprint), None)
    if not fingerprint or step is None:
        return fingerprint, []
    ticked = set(((task.get("data") or {}).get("ticks") or {}).get(fingerprint, ()))
    return fingerprint, [{"id": item["id"], "label": item["label"], "ticked": item["id"] in ticked}
                         for item in step["data"].get("checklist") or ()]


def approval(board, task: dict, state: Callable[[str], str | None] | None = None) -> dict | None:
    """The approval this task waits for now, if any: the newest open request for its current change set, unless the
    club is making a new version (you asked for changes) or the task is closed. With its checklist when it is the
    second check. `state` (the core's `approval_status`) rules out one decided elsewhere or expired with its run."""
    status = task["status"]
    if status in CLOSED:
        return None
    fingerprint, items = checklist(board, task)
    for item in reversed(board.messages(task["id"])):
        data = item.get("data") or {}
        if item.get("code") != "approval_request" or item.get("decided"):
            continue
        try:
            arguments = json.loads(data.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        capability = data.get("capability", "")
        if arguments.get("fingerprint") and arguments["fingerprint"] != fingerprint:
            continue  # for an older version
        if capability in ("workbench.review", "workbench.land") and status in ("working", "queued"):
            continue  # the new version brings its own check and its own final approval
        if state is not None and state(data.get("approval_id", "")) not in (None, "pending"):
            continue  # decided (typed, in another interface) or expired
        view = {"id": data.get("approval_id", ""), "capability": capability, "kind": data.get("kind", ""),
                "tier": data.get("tier", ""), "summary": data.get("summary") or item.get("text", ""),
                "command": arguments.get("command", ""), "message": item.get("id")}
        if capability == "workbench.review":
            view.update(fingerprint=fingerprint, checklist=items, remaining=sum(not entry["ticked"] for entry in items))
        return view
    return None


REPORT_WAIT_S = 120  # a report on its way for longer than this is taken as lost


def can_report(task: dict, now: float | None = None) -> bool:
    """Whether a report can go back now: the task came from another conversation, has an outcome, and was not
    reported since that outcome (nor is a report on its way)."""
    data = task.get("data") or {}
    status, reported, reporting = task["status"], data.get("reported") or {}, data.get("reporting") or {}
    if not data.get("origin_address") or status not in ("landed", "answered", "failed", "discarded"):
        return False
    if reporting and (now or time.time()) - float(reporting.get("at") or 0) < REPORT_WAIT_S:
        return False
    return not reported or reported.get("status", status) != status  # never, or before this outcome


def actions(task: dict, pending: dict | None) -> list[str]:
    """What can be done with the task now (the frontends offer exactly these)."""
    status, data = task["status"], task.get("data") or {}
    found = []
    if status == "invited":
        return ["accept", "discard"]
    if status not in CLOSED:
        found.append("send")
    if pending:
        found += ["check", "approve", "deny"] if pending["capability"] == "workbench.review" else ["approve", "deny"]
    if can_report(task):
        found.append("report")
    if status not in CLOSED and status not in ("working", "queued"):
        found.append("discard")
    if status in ("landed", "discarded", "answered", "failed", "cancelled") and not data.get("worktree"):
        found.append("forget")
    return found


def report_text(board, team, task: dict, link: Callable[[str], str] = lambda task: "") -> str:
    """The lab report that goes back to the conversation a task came from: what happened, who was really there."""
    data = task["data"]
    name = lambda persona: team.seats[persona].name if persona in team.seats else ""
    change, validation, landed = data.get("change") or {}, data.get("validation") or {}, data.get("landed")
    if landed:
        outcome = f"landed as {str(landed.get('commit', ''))[:8]} on {landed.get('branch', '')}"
    else:
        outcome = {"answered": "answered", "review": "ready for your check in the lab", "approved":
                   "checked, waiting for the final approval in the lab", "failed": "did not pass, nothing was applied",
                   "discarded": "discarded", "cancelled": "stopped"}.get(task["status"], task["status"])
    facts = [outcome]
    if change.get("stat") and change.get("stat") != "no changes":
        facts.append(change["stat"])
    if validation.get("ran"):
        facts.append(f"tests: {validation.get('summary')}")
    people = [person for person in (name(data.get("finished_by")), name((data.get("review") or {}).get("seat")))
              if person]
    body = str(data.get("summary") or data.get("answer") or data.get("failure") or "").strip()
    if len(body) > 1500:
        body = body[:1500].rsplit(" ", 1)[0] + " …"
    lines = [f"🧪 **Back from the lab: {task['title']}**", " · ".join(facts)
             + (f" ({' and '.join(people)})" if people else "")]
    # Who was really there, so the classroom remembers the visit as it was, not as it was planned.
    worked = [name(seat) for seat in dict.fromkeys(step["seat"] for step in board.steps(task["id"]) if step["seat"])]
    # whom the person took along (picked on arrival, none included), else whom the invitation suggested
    asked = data["crew"] if "crew" in data else (data.get("invited") or {}).get("crew") or []
    missing = [name(seat) for seat in asked if name(seat) and name(seat) not in worked]
    if any(worked):
        lines.append(f"In the lab: {', '.join(filter(None, worked))}."
                     + (f" Asked along but not there: {', '.join(missing)}." if missing else ""))
    if body:
        lines.append(body)
    if link(task["id"]):
        lines.append(f"The whole visit: {link(task['id'])}")
    return "\n\n".join(lines)


def task_view(board, task: dict, own_prefix: str, state: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    """A task as the frontends get it: its fields, the data they show, the approval it waits for, what they can do."""
    data = task.get("data") or {}
    keep = ("request", "route", "change", "validation", "review", "summary", "totals", "plan", "branch", "base",
            "worktree", "landed", "failure", "ticks", "reviewed", "baseline", "baseline_failed", "rounds", "by",
            "origin", "answer", "queued", "finished_by", "warning", "invited", "origin_address", "reported", "crew",
            "requirements", "verification", "reporting")
    view = {key: task[key] for key in ("id", "project", "address", "title", "status", "phase", "workflow",
                                       "created", "updated")}
    view["data"] = {key: data.get(key) for key in keep if data.get(key) is not None}
    view["data"]["totals"] = board.totals(task["id"])
    view["own"] = task["address"].startswith(own_prefix)
    view["approval"] = approval(board, task, state)
    view["actions"] = actions(task, view["approval"])
    return view

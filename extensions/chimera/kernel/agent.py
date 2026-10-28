"""The solver: one classmate working a task turn by turn with the workspace tools.

Each turn is one JSON object (say, note, plan, actions, status, summary). Actions run in order; their results
come back in the next turn. The loop ends when she says done or blocked, or after `max_turns`, when she gets one
last turn without tools to sum up where things stand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from contracts import ToolResult

from ..team import Seat
from . import prompts
from .context import Context, SeatUnavailable

NO_ACTIONS = ("No actions ran: you said continue but gave none. Act now (actions), or finish: status done (or "
              "blocked) with a summary.")
NO_CALLS = "No tools ran this turn: call them to act, or call finish when the task is done (or blocked)."
NOTHING_CHANGED = ("You finished, but no file differs from where the task started (the diff is empty). If the request "
                   "needs a change, make it now (actions); if nothing needs to change, finish again and say why.")

KEEP_FULL = 8          # the latest exchanges the model reads in full, at least; older results are shortened
OLD_RESULT_CHARS = 400


def short_count(count: int) -> int:
    """How many of the oldest exchanges are shortened: whole blocks of KEEP_FULL, so the start of the conversation
    stays the same for KEEP_FULL turns at a time and the provider's prompt cache keeps serving it. (Shortening one
    more every turn changed the start every turn: in the polyglot rounds, a quarter of turn 10's input and a seventh
    of turn 11's came from the cache, against over 90% on turns 3 to 9.)"""
    return max(0, count - KEEP_FULL) // KEEP_FULL * KEEP_FULL


@dataclass
class Exchange:
    """One turn and what came back: `results` as one message (the JSON protocol), or, for a classmate with tool calls,
    her own `answer` (words, calls, replay), each call's result in `outputs` and the club's `nudge` after them."""
    turn: dict
    results: str
    answer: dict | None = None
    outputs: dict = field(default_factory=dict)
    nudge: str = ""


@dataclass
class Solved:
    status: str        # done, blocked, limit
    summary: str
    seat: Seat
    provider: str
    turns: int
    findings: list = field(default_factory=list)  # the tester's: [{criterion, problem, evidence}]
    tier: str = "cheap"  # the models she worked with (a repair goes on with the same)


async def solve(ctx: Context, seat: Seat, tier: str, phase: str, *, mode: str, brief: str, write: bool,
                max_turns: int, role: str, project: str, test: str | None) -> Solved:
    probing = mode == "probe"  # the tester: reads, writes probe inputs, runs probes on the new code and the old
    names = prompts.PROBE_TOOLS if probing else prompts.WRITE_TOOLS if write else prompts.READ_TOOLS
    language = prompts.language_name(ctx.language)
    native = seat.tool_calls  # her API's own function calls instead of one JSON object per turn
    if probing:
        system = (prompts.TESTER_NATIVE if native else prompts.TESTER).format(
            voice=seat.voice, person=ctx.person, project=project, language=language, tools=prompts.tool_catalog(names),
            turns=max_turns)
    elif write:
        test_rule = (f"Run the tests (`{test}`) with run after changing code, and fix what fails." if test else
                     "This project has no tests yet. When you add code, add tests the usual way (unittest files named "
                     "tests/test_*.py with a tests/__init__.py, or pytest) and run them: the club runs them too.")
        system = (prompts.SOLVER_NATIVE if native else prompts.SOLVER).format(
            voice=seat.voice, person=ctx.person, project=project, role=prompts.ROLES[mode].format(person=ctx.person),
            test_rule=test_rule, language=language, tools=prompts.tool_catalog(names))
    elif mode == "plan":  # read-only, and the answer is a plan for a teammate
        system = (prompts.PLANNER_NATIVE if native else prompts.PLANNER).format(
            voice=seat.voice, person=ctx.person, project=project, language=language, tools=prompts.tool_catalog(names))
    else:
        system = (prompts.ASKER_NATIVE if native else prompts.ASKER).format(
            voice=seat.voice, person=ctx.person, project=project, language=language, tools=prompts.tool_catalog(names))
    history: list[Exchange] = []
    asked_empty = False  # a finish with nothing changed is questioned once (a model may only describe the work)
    label = {"draft": "drafting", "single": "working", "escalate": "taking over", "revise": "revising",
             "followup": "working", "repair": "fixing what the tester found", "execute": "carrying out the plan"
             }.get(mode, "looking into it") if write \
        else "trying to break it" if probing else "planning" if mode == "plan" else "looking into it"
    for number in range(1, max_turns + 2):
        final = number > max_turns
        step = f"{phase}.{number}"
        ctx.step(step, kind="turn", role=role, seat=seat.persona, status="running",
                 title=f"{seat.name} · {label}" + (f" · turn {number}" if number > 1 else ""),
                 data={"phase": phase, "mode": mode, "turn": number})
        await ctx.progress(f"{seat.name} · {label}…", key=f"{step}.m")
        # The same tools and schema on every turn, the last one included: they come before the conversation (the
        # tools in the request, the schema in the system prompt), so offering fewer would void the prompt cache for
        # the whole turn. The last turn is told to finish, and actions it asks for anyway are not run (below).
        if native:
            messages, schema = _native_messages(system, brief, history, final), None
            tools = prompts.tool_specs(names, probing=probing, planning=write)
        else:
            messages, schema, tools = _messages(system, brief, history, final), prompts.turn_schema(
                names, findings=probing), ()
        try:
            answer = await ctx.model(f"{step}.m", seat, tier, messages, schema=schema, schema_name="chimera_turn",
                                     effort=str(ctx.settings.get("turn_effort", "medium")),
                                     max_tokens=int(ctx.settings.get("turn_tokens", 8000)),
                                     timeout_s=float(ctx.settings.get("turn_timeout_s", 420)), step=step, role=role,
                                     tools=tools)
        except SeatUnavailable as error:  # the step ends here (someone else takes over, or the round stops)
            ctx.step(step, status="failed", data={"error": error.reason})
            raise
        native_turn = answer.get("data") is None and answer.get("tool_calls") is not None  # (a replay keeps its kind)
        if native_turn:  # her words and calls, as a turn
            turn = _native_turn(answer, probing)
        else:
            turn = {"say": "", "note": "", "summary": "", **answer["data"]}
            if "status" not in turn:  # it follows from the actions; with none given at all, a turn used (not the end)
                turn["status"] = "continue" if turn.get("actions") or ("actions" not in turn and not final) else "done"
            turn.setdefault("actions", [])
        # A finish beside other calls: they run first, and the finish stands only if they all worked.
        finish_after = turn["status"] if native_turn and turn["status"] != "continue" and turn["actions"] and \
            not final else ""
        if finish_after:
            turn["status"] = "continue"
        author = ctx.team.seats.get(answer.get("seat", ""), seat)  # a replayed turn stays its author's
        ctx.step(step, seat=author.persona, title=f"{author.name} · {label}" + (f" · turn {number}" if number > 1 else ""),
                 data={key: turn.get(key) for key in ("say", "note", "plan", "status", "summary", "findings")
                       if key != "findings" or probing})
        if turn.get("plan"):
            ctx.update(data={"plan": turn["plan"]})
        await ctx.say(f"{step}.say", author, turn.get("say", ""))
        own = answer if native_turn else None
        if not final and turn["status"] == "continue" and not turn["actions"]:  # (a turn used, not the work ended)
            if own and own.get("tool_calls"):  # only her plan: noted, nothing to remind her of
                history.append(Exchange(turn, "Plan noted.", own, _noted(own)))
            else:
                nudge = NO_CALLS if own else NO_ACTIONS
                history.append(Exchange(turn, nudge, own, _noted(own), nudge))
            ctx.step(step, status="done")
            continue

        async def nothing_changed(after: str) -> bool:
            """A first finish with an empty diff is questioned once (a model may only describe the work)."""
            nonlocal asked_empty
            if not write or final or asked_empty:
                return False
            asked_empty = True
            looked = await ctx.tool(f"{step}.empty{after}", "diff", {})
            return looked.status == "success" and not looked.data.get("files")
        if turn["status"] == "done" and await nothing_changed(""):
            history.append(Exchange(turn, NOTHING_CHANGED, own, _noted(own), NOTHING_CHANGED))
            ctx.step(step, status="done")
            continue
        if final or turn["status"] != "continue":
            ctx.step(step, status="done")
            return _solved(turn, seat, answer, number, probing, tier)
        results: list[tuple[dict, ToolResult]] = []
        for index, action in enumerate(turn["actions"][:6]):
            arguments = dict(action.get("arguments") or {})
            arguments.pop("task", None)
            if action["tool"] not in names:
                result = ToolResult.fail(f"{action['tool']} is not available in this step", "unknown_tool")
            else:
                await ctx.progress(f"{seat.name} · {_doing(action['tool'], arguments)}", key=f"{step}.a{index}")
                result = await ctx.tool(f"{step}.a{index}", action["tool"], arguments, step=step)
            results.append((action, result))
            if result.status != "success":
                break
        outputs = {**_noted(own), **{action["id"]: _result_text(action, result) for action, result in results
                                     if action.get("id")}}
        exchange = Exchange(turn, _results(results, len(turn["actions"])), own, outputs)
        history.append(exchange)
        ctx.step(step, status="done")
        if finish_after and all(result.status == "success" for _, result in results) and \
                len(results) == len(turn["actions"]):
            turn["status"] = finish_after
            if finish_after == "done" and await nothing_changed(".after"):
                exchange.nudge = NOTHING_CHANGED
                continue
            return _solved(turn, seat, answer, number, probing, tier)
        if finish_after:
            exchange.nudge = ("finish was not taken: not every call before it ran (one failed, or there were more "
                              "than 6). Look at the results, then go on or call finish alone.")
    raise AssertionError("unreachable")


def _solved(turn: dict, seat: Seat, answer: dict, number: int, probing: bool, tier: str) -> Solved:
    status = turn["status"] if turn["status"] != "continue" else "limit"
    return Solved(status, turn.get("summary") or turn.get("note") or "", seat, answer["provider"], number,
                  [dict(item) for item in turn.get("findings") or ()] if probing else [], tier)


def _native_turn(answer: dict, probing: bool) -> dict:
    """A tool-calling answer as the turn the club reads: her words are the note, finish gives status and summary,
    plan the checklist, and the other calls are the actions (each keeping its call's id)."""
    turn = {"say": "", "note": (answer.get("text") or "").strip(), "summary": "", "actions": [], "status": "continue"}
    for call in answer.get("tool_calls") or ():
        arguments = dict(call.get("arguments") or {})
        if call["name"] == "finish":
            turn.update(status=arguments.get("status") if arguments.get("status") in ("done", "blocked") else "done",
                        summary=str(arguments.get("summary") or ""), say=str(arguments.get("say") or ""))
            if probing:
                turn["findings"] = [dict(item) for item in arguments.get("findings") or () if isinstance(item, dict)]
        elif call["name"] == "plan":
            turn["plan"] = [dict(item) for item in arguments.get("steps") or () if isinstance(item, dict)]
        else:
            turn["actions"].append({"tool": call["name"], "arguments": arguments, "id": call["id"]})
    return turn


def _noted(answer: dict | None) -> dict:
    """The results of the calls that are not actions (plan, finish) and of those never run, by call id."""
    if not answer:
        return {}
    return {call["id"]: "noted" if call["name"] in ("plan", "finish") else
            "not run: an earlier call in this turn failed, or there were more than 6"
            for call in answer.get("tool_calls") or ()}


def _doing(tool: str, arguments: dict) -> str:
    target = arguments.get("path") or arguments.get("command") or arguments.get("pattern") or ""
    return f"{tool} {str(target)[:60]}".strip()


def _result_text(action: dict, result: ToolResult) -> str:
    state = "ok" if result.status == "success" else f"failed ({result.error_code or result.status})"
    return f"{state}\n{result.summary}"


def _results(results: list[tuple[dict, ToolResult]], planned: int) -> str:
    lines = ["Results of your actions:"]
    for number, (action, result) in enumerate(results, 1):
        shown = json.dumps(action.get("arguments") or {}, ensure_ascii=False)
        if len(shown) > 200:
            shown = shown[:200] + "…}"
        lines.append(f"[{number}] {action['tool']} {shown} → {_result_text(action, result)}")
    if planned > len(results):
        lines.append(f"({planned - len(results)} later action(s) not run because this one failed.)")
    return "\n\n".join(lines)


def _text(exchange: Exchange) -> str:
    """What came back after a turn, as one message: the results, then the club's word if it is another."""
    if not exchange.nudge or exchange.nudge == exchange.results:
        return exchange.results
    return f"{exchange.results}\n\n{exchange.nudge}"


def _native_messages(system: str, brief: str, history: list[Exchange], final: bool) -> list[dict]:
    """The exchanges as her API's own messages: her words and calls (with their replay), each call's result as a
    tool message, the club's word as a user message. A classmate's turns before hers (another protocol, before a
    takeover) come as one note, never as her own messages."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
    earlier = [exchange for exchange in history if exchange.answer is None]
    if earlier:
        notes = [json.dumps(_compact(exchange.turn), ensure_ascii=False) + "\n" + _shortened(_text(exchange))
                 for exchange in earlier]
        messages.append({"role": "user", "content": "A classmate worked on this before you:\n\n" + "\n\n".join(notes)})
    own = [exchange for exchange in history if exchange.answer is not None]
    old = short_count(len(own))
    for index, exchange in enumerate(own):
        calls = exchange.answer.get("tool_calls") or []
        shown = calls if index >= old else [  # an older exchange: what each call was for, results shortened
            {**call, "arguments": {key: value for key, value in (call.get("arguments") or {}).items()
                                   if key in ("path", "command", "pattern", "status")}} for call in calls]
        messages.append({"role": "assistant", "content": exchange.answer.get("text") or "", "tool_calls": shown,
                         "replay": exchange.answer.get("replay")})
        for call in calls:
            output = exchange.outputs.get(call["id"], "not run")
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": output if index >= old else _shortened(output)})
        if exchange.nudge:
            messages.append({"role": "user", "content": exchange.nudge})
    if final:
        messages.append({"role": "user", "content": "You have used all your turns. Call finish now, with status done "
                         "or blocked and a summary of where things stand (what is changed, what is checked, what is "
                         "left)."})
    return messages


def _compact(turn: dict) -> dict:
    """What a turn did, shortened (an older exchange): its note, status and the actions' targets."""
    compact = {key: turn.get(key) for key in ("note", "status")}
    compact["actions"] = [{"tool": item["tool"], "arguments": {key: value for key, value in
                           (item.get("arguments") or {}).items() if key in ("path", "command", "pattern")}}
                          for item in turn.get("actions") or ()]
    return compact


def _shortened(text: str) -> str:
    return text[:OLD_RESULT_CHARS] + ("\n… (older result shortened; read the file again if you need it)"
                                      if len(text) > OLD_RESULT_CHARS else "")


def _messages(system: str, brief: str, history: list[Exchange], final: bool) -> list[dict]:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": brief}]
    old = short_count(len(history))
    for index, exchange in enumerate(history):
        turn = {**exchange.turn, "actions": [{key: value for key, value in action.items() if key != "id"}
                                             for action in exchange.turn.get("actions") or ()]}
        results = _text(exchange)
        if index < old:  # an older exchange: what she did, shortened
            messages.append({"role": "assistant", "content": json.dumps(_compact(turn), ensure_ascii=False)})
            messages.append({"role": "user", "content": _shortened(results)})
        else:
            messages.append({"role": "assistant", "content": json.dumps(turn, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
    if final:
        messages[-1] = {"role": "user", "content": messages[-1]["content"] + (
            "\n\nYou have used all your turns. Return status done or blocked now, with no actions, and a summary of "
            "where things stand (what is changed, what is checked, what is left).")}
    return messages

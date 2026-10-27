"""One run of the workbench engine: its journal, and the only doors to models, tools, speech and the board.

The engine is replayed from the top after every restart and every approval (the core's durable-run model). Each
step that costs something or changes something (a model call, a tool call, a line said to the person) is kept in
the run's state under a key the code computes the same way each time; replaying returns the kept result instead
of doing it again. So the workflows read as plain sequential code and still survive a restart or a wait of days
for an approval:

    journal  r1.route.m      → the router's answer          (a model call)
             r1.draft.1.m    → DeepSeek's first turn
             r1.draft.1.a0   → read_file result            (a tool call; call_id = the key)
             r1.draft.1.say  → true                        (said once, never twice)
             r1.review.ask   → (waiting: the run yields; the decision arrives as a tool_result event)
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from contracts import ModelError, ModelRequest, Session, ToolResult

from ..team import Seat, Team, family_of


class Suspend(Exception):
    """A tool call waits for the creator's approval: the run yields and resumes when it is decided."""

    def __init__(self, approval_id: str, key: str):
        super().__init__(approval_id)
        self.approval_id, self.key = approval_id, key


class SeatUnavailable(Exception):
    """This seat's models cannot be called (no key): another classmate takes the role."""

    def __init__(self, seat: Seat, reason: str, stuck: bool = False):
        super().__init__(reason)
        self.seat, self.reason, self.stuck = seat, reason, stuck  # stuck: she answered, but could not finish


class Context:
    def __init__(self, session: Session, board, team: Team, settings: Mapping[str, Any], link=None, prices=None):
        self.session, self.board, self.team, self.settings = session, board, team, settings
        self.link = link or (lambda task: "")  # the task's address in the workbench page, when it is up
        self.prices = prices                   # a PriceBook (prices.py); without one, costs are unknown
        self.state = dict(session.state or {})
        self.done: dict[str, Any] = self.state.setdefault("journal", {})
        self.task_id: str = self.state.get("task", "")
        self.present = {persona.name for persona in session.personas}
        self.language = session.trigger.text
        self.person = session.trigger.sender.name or "the person"
        self._calls: dict[str, dict] = {}    # step → {journal key: call row}: a replay overwrites, never adds
        self._actions: dict[str, dict] = {}  # step → {journal key: action line}
        self.staffing: str | None = None  # while set, a fresh answer records its seat under this key (replays keep her)
        self.commands: list[str] = []     # shell lines run in this pass, for the checklist
        self._absorb()

    # ---- the journal ----------------------------------------------------------------------------
    def _absorb(self) -> None:
        """Decisions on approvals arrive as events: an approved call's result, or a denial."""
        for event in self.session.events:
            if event.kind not in ("tool_result", "approval_denied"):
                continue
            key = str(event.data.get("call_key", "")).removeprefix("call:")
            if not key or key in self.done:
                continue
            if event.kind == "tool_result":
                self.done[key] = dict(event.data["result"])
            else:
                self.done[key] = ToolResult.fail("The creator denied this; it did not run.", "denied").to_dict()

    async def save(self) -> None:
        if self.task_id:
            self.state["task"] = self.task_id
        await self.session.save(self.state)

    def kept(self, key: str) -> bool:
        return key in self.done

    async def once(self, key: str, factory) -> Any:
        """`factory()`'s value, computed on the first pass only."""
        if key in self.done:
            return self.done[key]
        value = await factory()
        self.done[key] = value
        await self.save()
        return value

    # ---- tools ----------------------------------------------------------------------------------
    async def tool(self, key: str, name: str, arguments: Mapping[str, Any] | None = None,
                   step: str | None = None) -> ToolResult:
        """`workbench.<name>` for this task, through the core's policy. Waiting for approval suspends the run."""
        arguments = dict(arguments or {})
        if key in self.done:
            result = ToolResult.from_dict(self.done[key])
        else:
            result = await self.session.tools.call(f"workbench.{name}", {**arguments, "task": self.task_id},
                                                   call_id=key)
            if result.status == "needs_approval":
                if step:
                    self._note_action(step, key, name, arguments, None, waiting=str(result.data.get("approval_id", "")))
                raise Suspend(str(result.data.get("approval_id", "")), key)
            self.done[key] = result.to_dict()
            await self.save()
        if step:
            self._note_action(step, key, name, arguments, result)
        return result

    def _note_action(self, step: str, key: str, name: str, arguments: Mapping[str, Any], result: ToolResult | None,
                     waiting: str = "") -> None:
        entries = self._actions.setdefault(step, {})
        entry = {"tool": name, "arguments": {item: _short(value) for item, value in arguments.items()}}
        if result is None:
            entry.update(status="waiting", approval=waiting)
        else:
            entry.update(status=result.status, error=result.error_code or "", summary=result.summary[:4000],
                         data={item: value for item, value in result.data.items()
                               if item in ("exit_code", "seconds", "path", "added", "removed", "created", "line",
                                           "timed_out", "matches", "passed", "same")})
        if name == "run" and result is not None and arguments.get("command") and key not in entries:
            self.commands.append(str(arguments["command"])[:200])
        entries[key] = entry  # the approved call's result replaces its waiting line
        self.step(step, data={"actions": list(entries.values())})

    # ---- models ---------------------------------------------------------------------------------
    async def model(self, key: str, seat: Seat, tier: str, messages: list[dict], *, schema: Mapping | None = None,
                    schema_name: str = "output", effort: str = "medium", max_tokens: int = 4000,
                    timeout_s: float = 120.0, step: str | None = None, role: str = "", tools=()) -> dict:
        """One call to `seat`'s models of `tier`. An answer that is not the requested JSON (or, with `tools`, whose
        calls do not fit them) is corrected once."""
        effort = seat.capped(effort)
        if key in self.done:
            answer = self.done[key]
        else:
            answer = await self._generate(seat, tier, messages, schema, schema_name, effort, max_tokens, timeout_s,
                                          tools)
            answer["seat"] = seat.persona  # whose answer it is, even when a replay is driven by whoever took over
            answer["effort"] = effort  # and how hard she thought (the one replaying may have another limit)
            # Priced now and kept with the answer: a later price list never rewrites what a past step cost.
            answer["price"] = self.prices.price(answer["provider"], answer["model"]) if self.prices else None
            self.done[key] = answer
            if self.staffing:
                self.done[self.staffing] = seat.persona
            await self.save()
        if step:
            author = self.team.seats.get(answer.get("seat", ""), seat)
            self._calls.setdefault(step, {})[key] = self._call_row(answer, author, tier, role,
                                                                   answer.get("effort", effort))
            self.step(step, data={"calls": list(self._calls[step].values())})
        return answer

    async def _generate(self, seat, tier, messages, schema, schema_name, effort, max_tokens, timeout_s,
                        tools=()) -> dict:
        request = ModelRequest(messages, profile=seat.profile(tier), output_schema=schema, schema_name=schema_name,
                               effort=effort, max_tokens=max_tokens, timeout_s=timeout_s, persona=seat.persona
                               if seat.persona in self.present else None, tools=tuple(tools))
        started = time.monotonic()
        try:
            result = await self.session.models.generate(request)
        except ModelError as error:
            if error.kind in ("not_configured", "unavailable"):  # no key, or her provider is down: someone else
                if error.kind == "not_configured":
                    self.team.mark_unavailable(seat.persona, _reason(str(error)))
                raise SeatUnavailable(seat, _reason(str(error))) from None
            if error.kind != "invalid_output" or (schema is None and not tools):
                raise
            # One correction, as the room does: say what was wrong and ask for the whole object again. An answer cut
            # off at the length limit is a budget problem, not a format one: twice the room, and less per turn.
            cut = "truncated" in str(error)
            fixed = [*messages, {"role": "user", "content": (
                "Your previous reply was cut off at the length limit. Do less in this turn: one or two actions, and "
                "write a big file in parts (write_file with append: true), then continue next turn. "
                + ("Call the tools again." if tools else "Return one JSON object that matches the schema.") if cut else
                f"Your previous reply could not be used: {error}. Call the tools with arguments that fit them, or "
                "call finish; keep it short (fewer or smaller calls)." if tools else
                f"Your previous reply was not valid: {error}. Return one JSON object that matches the schema, and "
                "keep it short (fewer or smaller actions).")}]
            try:
                result = await self.session.models.generate(ModelRequest(  # (cut off: less hidden reasoning too)
                    fixed, profile=request.profile, output_schema=schema, schema_name=schema_name,
                    effort="low" if cut else effort,
                    max_tokens=min(max_tokens * 2, int(self.settings.get("max_tokens_cap", 32000))) if cut
                    else max_tokens, timeout_s=timeout_s * (1.5 if cut else 1), persona=request.persona,
                    tools=request.tools))
            except ModelError as again:
                if again.kind in ("not_configured", "unavailable", "invalid_output"):  # stuck: a classmate takes over
                    reason = ("her answer was cut off twice, too long" if "truncated" in str(again) else
                              "her answer could not be read" if again.kind == "invalid_output" else _reason(str(again)))
                    raise SeatUnavailable(seat, reason, stuck=True) from None
                raise
        return {"text": result.text, "data": result.data, "provider": result.provider, "model": result.model,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cached_input_tokens": getattr(result, "cached_input_tokens", 0),
                "tool_calls": [{"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                               for call in getattr(result, "tool_calls", ())],
                "replay": dict(result.replay) if getattr(result, "replay", None) else None,
                "seconds": round(result.duration_s or time.monotonic() - started, 3),
                "fallbacks": list(result.fallbacks)}

    def _call_row(self, answer: dict, seat: Seat, tier: str, role: str, effort: str) -> dict:
        return {"seat": seat.persona, "role": role, "tier": tier, "effort": effort, "provider": answer["provider"],
                "model": answer["model"], "family": self.family(answer["provider"]),
                "input_tokens": answer["input_tokens"], "output_tokens": answer["output_tokens"],
                "cached_input_tokens": answer.get("cached_input_tokens", 0), "seconds": answer["seconds"], "cost": self.cost(answer), "price": answer.get("price"),
                "fallbacks": answer.get("fallbacks", [])}

    def crew(self) -> list[Seat]:
        """The classmates who came along to the Classroom for this task (besides the one who brought you, who leads and
        keeps her usual roles); empty when nobody was chosen."""
        data = (self.task() or {}).get("data") or {} if self.task_id else {}
        invited = data.get("invited") or {}
        chosen = data.get("crew", invited.get("crew")) or []
        return [self.team.seats[name] for name in dict.fromkeys(chosen) if name in self.team.seats
                and name in self.present and name != invited.get("lead")]

    def candidates(self, role: str) -> list[Seat]:
        """The seats for `role` in order, the crew who came along first."""
        return self.team.candidates(role, self.present, prefer={seat.persona for seat in self.crew()})

    def able(self, seat: Seat) -> bool:
        """Whether `seat` can be expected to answer: asked of the models where they can tell (on its own), else
        whether she was found without a key lately."""
        ready = getattr(self.session.models, "ready", None)
        if callable(ready):
            return bool(ready(seat.profile("cheap")))
        return not self.team.known_unavailable(seat.persona)

    def family(self, provider: str) -> str:
        return family_of(provider, self.settings.get("families"))

    @staticmethod
    def cost(answer: dict) -> float | None:
        """Dollars at the price the answer was given when it came; None when no price was known."""
        price = answer.get("price")
        if not price:
            return None
        return round(answer["input_tokens"] * float(price["input"]) / 1e6
                     + answer["output_tokens"] * float(price["output"]) / 1e6, 6)

    # ---- speaking -------------------------------------------------------------------------------
    async def say(self, key: str, seat: Seat | None, text: str, label: str = "") -> None:
        """Say `text` once as `seat` (at most once: kept before it is sent)."""
        text = str(text or "").strip()
        if not text or key in self.done:
            return
        self.done[key] = True
        await self.save()
        persona = seat.persona if seat is not None and seat.persona in self.present else None
        await self.session.reply(text, persona=persona, label=label)

    async def progress(self, text: str, key: str | None = None) -> None:
        if key is not None and key in self.done:
            return  # replaying: that moment has passed
        await self.session.progress(text)

    # ---- the board ------------------------------------------------------------------------------
    def step(self, step_id: str, **fields: Any) -> None:
        if self.task_id:
            self.board.put_step(self.task_id, step_id, **fields)

    def update(self, **fields: Any) -> dict | None:
        return self.board.update_task(self.task_id, **fields) if self.task_id else None

    def task(self) -> dict:
        return self.board.task(self.task_id) or {}

    def owns(self) -> bool:
        """Whether this run still owns the task (a newer message can take it over while this one waits)."""
        owner = (self.task().get("data") or {}).get("owner") or {}
        return owner.get("run") in (None, self.session.run_id)


def _short(value: Any) -> Any:
    if isinstance(value, str) and len(value) > 300:
        return value[:300] + f"… ({len(value)} characters)"
    return value


def _reason(text: str) -> str:
    """`No model candidate answered: claude: not configured (CLAUDE_API_KEY is not set)` → the key's name."""
    if "is not set" in text:
        return "no " + text.split("(")[-1].split(" is not set")[0].strip() if "(" in text else "no key"
    return "not configured" if "not configured" in text else "her model did not answer"

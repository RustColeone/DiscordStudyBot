"""Reference single-persona engine.

Each step asks the model for one JSON turn (message, status, actions), speaks, then runs the actions
through the core's tools handle. All progress is checkpointed with `session.save`, and every action
carries a stable call_id, so a restart resumes without repeating work.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from contracts import Engine, EngineSpec, ModelError, ModelRequest, RunYield, Session, ToolResult

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "status": {"type": "string", "enum": ["continue", "complete", "needs_input", "blocked"]},
        "actions": {"type": "array", "maxItems": 5, "items": {
            "type": "object",
            "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["tool", "arguments"], "additionalProperties": False}},
    },
    "required": ["message", "status", "actions"],
    "additionalProperties": False,
}

CONTRACT = """\
Respond to every turn with one JSON object: {{"message": string, "status": "continue"|"complete"|"needs_input"|"blocked", "actions": [{{"tool": name, "arguments": {{...}}}}]}}.
- message: what you say to the person now, in their language and your own voice. Keep it brief; it may be empty while acting.
- With actions, the message only says what you are about to do. Never state an action's outcome (saved, sent, done) before its [tool] result appears.
- continue: you run the listed actions now and see their results next turn. continue requires at least one action.
- complete: the request is handled. needs_input: you asked one question and wait for the answer. blocked: you cannot finish; say why briefly. These take no actions.
- Tool results appear as notes starting with [tool]. Never claim a result you have not seen.
- Files from successful tools are delivered to the person automatically; do not deliver them again.
- An action may wait for the owner's approval. If so, tell the person briefly that it is waiting.
{display}
Available tools:
{tools}"""


class AssistantEngine(Engine):
    spec = EngineSpec("assistant", "One persona that talks, uses tools and asks questions; resumable.", "one")

    def __init__(self, max_steps: int = 8, history_limit: int = 16, effort: str = "low", max_tokens: int = 1200):
        self.max_steps, self.history_limit, self.effort, self.max_tokens = max_steps, history_limit, effort, max_tokens

    async def run(self, session: Session) -> RunYield:
        state = session.state or {"transcript": [], "pending": [], "event_cursor": -1, "steps": 0,
                                  "call_seq": 0, "quote": None}
        outcome = await self._absorb_events(session, state)
        if outcome is not None:
            return outcome
        while True:
            for request in session.inbox():
                self._add_input(session, state, request.text, request.sender.name or request.sender.id, request.id)
            if state["steps"] >= self.max_steps:
                await self._close(session, state)
                return RunYield.DONE
            state["steps"] += 1
            turn = await self._turn(session, state)
            state["transcript"].append({"role": "assistant", "content": json.dumps(turn, ensure_ascii=False)})
            for action in turn["actions"]:
                state["call_seq"] += 1
                state["pending"].append({"call_id": f"c{state['call_seq']}", "tool": action["tool"],
                                         "arguments": action["arguments"]})
            await session.save(state)
            if turn["message"].strip():
                await session.reply(turn["message"].strip(), quote=state.get("quote"))
            if state["pending"]:
                outcome = await self._execute_pending(session, state)
                if outcome is not None:
                    return outcome
                continue
            if turn["status"] == "needs_input":
                return RunYield.WAIT_INPUT
            return RunYield.DONE

    # ---- events and actions --------------------------------------------------------------------
    def _add_input(self, session: Session, state: dict, text: str, sender: str, request_id: str) -> None:
        prefix = "" if session.trigger.is_direct else f"{sender}: "
        state["transcript"].append({"role": "user", "content": prefix + text})
        if session.display.quotes and not session.trigger.is_direct:
            # quote: latest (default) follows the newest message; trigger keeps quoting the original request.
            if session.options.get("quote", "latest") != "trigger" or not state.get("quote"):
                state["quote"] = request_id

    async def _absorb_events(self, session: Session, state: dict) -> RunYield | None:
        deliveries = []
        for event in session.events:
            if event.index <= state["event_cursor"]:
                continue
            state["event_cursor"] = event.index
            if event.kind == "input":
                request = event.data["request"]
                sender = request["sender"].get("name") or request["sender"]["id"]
                self._add_input(session, state, request["text"], sender, request["id"])
            elif event.kind in ("tool_result", "approval_denied"):
                call_id = str(event.data.get("call_key", "")).removeprefix("call:")
                action = next((item for item in state["pending"] if item["call_id"] == call_id), None)
                if action is None:
                    continue
                if event.kind == "tool_result":
                    result = ToolResult.from_dict(event.data["result"])
                    state["pending"].remove(action)
                    self._note(state, action, result)
                    if result.status == "success" and result.files:
                        deliveries.append(result)
                    if result.status != "success":
                        state["pending"].clear()
                else:
                    state["pending"].clear()
                    state["transcript"].append({"role": "user",
                                                "content": f"[tool {action['tool']}] The owner denied this action; it did not run."})
        await session.save(state)
        for result in deliveries:
            await self._send_files(session, state, result)
        if state["pending"]:
            return await self._execute_pending(session, state)
        return None

    async def _execute_pending(self, session: Session, state: dict) -> RunYield | None:
        while state["pending"]:
            action = state["pending"][0]
            result = await session.tools.call(action["tool"], action["arguments"], call_id=action["call_id"])
            if result.status == "needs_approval":
                if not action.get("awaiting"):
                    action["awaiting"] = result.data.get("approval_id", "")
                    state["transcript"].append({"role": "user", "content": f"[tool {action['tool']}] {result.summary}"})
                await session.save(state)
                return RunYield.WAIT_APPROVAL
            state["pending"].pop(0)
            self._note(state, action, result)
            if result.status != "success":
                state["pending"].clear()
            await session.save(state)
            if result.status == "success" and result.files:
                await self._send_files(session, state, result)
        return None

    def _note(self, state: dict, action: dict, result: ToolResult) -> None:
        detail = result.summary.strip()[:3000]
        code = f" ({result.error_code})" if result.error_code and result.status != "success" else ""
        state["transcript"].append({"role": "user", "content": f"[tool {action['tool']} → {result.status}{code}] {detail}"})

    async def _send_files(self, session: Session, state: dict, result: ToolResult) -> None:
        sent = []
        for file in result.files:
            delivery = await session.send_file(file)
            sent.append(f"{file.name or file.path}: {'delivered' if delivery.ok else 'not delivered (' + delivery.detail + ')'}")
        state["transcript"].append({"role": "user", "content": "[tool files] " + "; ".join(sent)})
        await session.save(state)

    # ---- model turns -----------------------------------------------------------------------------
    def _messages(self, session: Session, state: dict, correction: str = "") -> list[dict[str, str]]:
        persona = session.personas[0]
        display = session.display
        style = ("You may use Markdown." if display.markdown else "Use plain text only; no Markdown.")
        tools = [{key: tool[key] for key in ("name", "description", "parameters", "risk")}
                 for tool in session.tools.list()]
        instructions = str(session.options.get("instructions") or "").strip()
        now = dt.datetime.fromtimestamp(session.trigger.received_at).astimezone()
        system = (f"{persona.prompt.strip()}\n\nYour name here is {persona.display_name}. "
                  f"The current time is {now.strftime('%A %Y-%m-%d %H:%M')} (UTC{now.strftime('%z')}).\n\n"
                  + (f"Instructions for this conversation:\n{instructions}\n\n" if instructions else "")
                  + CONTRACT.format(display=f"{style} Keep each message under {display.max_text_chars} characters.",
                                    tools=json.dumps(tools, ensure_ascii=False) if tools else "(none)"))
        activity = self._activity(session)
        if activity:
            system += ("\n\nOther requests in this conversation are being handled in parallel. Do not start the same "
                       "work again; if asked about it, say it is still in progress:\n" + activity)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        started = session.trigger.received_at
        items = [item for item in session.history.recent(self.history_limit * 3) if item.at < started]
        if session.options.get("history_scope") == "sender" and not session.trigger.is_direct:
            # Only this member's own messages and the replies that answered them.
            sender = session.trigger.sender.id
            theirs = {item.message_id for item in items if item.direction == "in" and item.sender_id == sender}
            items = [item for item in items if (item.direction == "in" and item.sender_id == sender)
                     or (item.direction == "out" and item.reply_to in theirs)]
        for item in items[-self.history_limit:]:
            role = "user" if item.direction == "in" else "assistant"
            text = item.text if role == "assistant" or session.trigger.is_direct else f"{item.sender_name or item.sender_id}: {item.text}"
            messages.append({"role": role, "content": text})
        messages.extend(dict(item) for item in state["transcript"])
        if correction:
            messages.append({"role": "user", "content": correction})
        merged: list[dict[str, str]] = []
        for message in messages:
            if merged and merged[-1]["role"] == message["role"] and message["role"] != "system":
                merged[-1] = {"role": message["role"], "content": merged[-1]["content"] + "\n\n" + message["content"]}
            else:
                merged.append(message)
        if len(merged) == 1 or merged[1]["role"] != "user":
            merged.insert(1, {"role": "user", "content": "(conversation continues)"})
        return merged

    @staticmethod
    def _activity(session: Session) -> str:
        import time
        items = session.activity()
        if session.options.get("history_scope") == "sender" and not session.trigger.is_direct:
            items = [item for item in items if item.sender_id == session.trigger.sender.id]
        lines = []
        for item in items[:5]:
            doing = f"running {item.doing}" if item.doing else item.status.replace("_", " ")
            lines.append(f"- {item.sender_name or item.sender_id}: \"{item.request[:120]}\" ({doing}, "
                         f"{int(max(0, time.time() - item.since))}s)")
        return "\n".join(lines)

    async def _turn(self, session: Session, state: dict, correction: str = "") -> dict[str, Any]:
        names = {tool["name"] for tool in session.tools.list()}
        for attempt in range(3):
            try:
                result = await session.models.generate(ModelRequest(
                    self._messages(session, state, correction), output_schema=TURN_SCHEMA, schema_name="turn",
                    effort=self.effort, max_tokens=self.max_tokens))
            except ModelError as error:
                if error.kind != "invalid_output" or attempt == 2:
                    raise
                correction = f"Your previous reply was not valid: {error}. Reply with one JSON object matching the turn format."
                continue
            turn = result.data
            problem = self._problem(turn, names)
            if problem is None:
                return turn
            correction = f"Your previous turn was rejected: {problem} Send a corrected turn."
        raise ModelError("invalid_output", "The model did not produce a valid turn after corrections")

    @staticmethod
    def _problem(turn: dict, names: set[str]) -> str | None:
        unknown = [action["tool"] for action in turn["actions"] if action["tool"] not in names]
        if unknown:
            return f"unknown tools {unknown}; use only the listed tools."
        if turn["status"] == "continue" and not turn["actions"]:
            return "status continue requires at least one action."
        if turn["status"] != "continue" and turn["actions"]:
            return f"status {turn['status']} takes no actions."
        if turn["status"] in ("needs_input", "blocked") and not turn["message"].strip():
            return f"status {turn['status']} needs a message for the person."
        return None

    async def _close(self, session: Session, state: dict) -> None:
        correction = ("You must stop now and cannot run more actions. Reply with status blocked and no actions: "
                      "briefly tell the person what is done and what is unfinished.")
        try:
            result = await session.models.generate(ModelRequest(
                self._messages(session, state, correction), output_schema=TURN_SCHEMA, schema_name="turn",
                effort=self.effort, max_tokens=self.max_tokens))
            message = (result.data or {}).get("message", "").strip()
        except ModelError:
            message = ""
        if message:
            await session.reply(message, quote=state.get("quote"))

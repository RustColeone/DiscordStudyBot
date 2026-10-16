"""One task owner decides, acts, observes, and continues across any registered tools."""
from dataclasses import dataclass, field
import json

from services.request_timing import record as record_timing
from services.tool_outcome import ToolOutcome, normalize_outcome


@dataclass
class AgentStep:
    status: str
    message: str
    actions: list[dict] = field(default_factory=list)


def parse_step(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("Missing agent step JSON")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict) or set(value) != {"status", "message", "actions"}:
        raise ValueError("A step must contain status, message, and actions")
    if value["status"] not in {"continue", "complete", "needs_input", "blocked"}:
        raise ValueError("Invalid step status")
    if not isinstance(value["message"], str) or not value["message"].strip():
        raise ValueError("Every step needs a visible message")
    if not isinstance(value["actions"], list):
        raise ValueError("Step actions must be a list")
    if (value["status"] == "continue") != bool(value["actions"]):
        raise ValueError("Only continue steps may include actions, and must include at least one")
    return AgentStep(value["status"], value["message"].strip(), value["actions"])


@dataclass
class AgentRun:
    task: object
    owner: str = "DeepSeek"
    status: str = "running"
    steps: int = 0
    actions_taken: int = 0
    observations: list[dict] = field(default_factory=list)
    evidence_images: list[dict] = field(default_factory=list)
    pending_actions: list[dict] = field(default_factory=list)
    approved: set[str] = field(default_factory=set)
    writes_completed: set[str] = field(default_factory=set)
    visible_turns: list[str] = field(default_factory=list)
    user_answers: list[str] = field(default_factory=list)
    original_time: object = None
    last_step: AgentStep | None = None
    input_required: bool = False

    def context(self):
        return {"goal": self.task.goal, "status": self.status,
                "observations": self.observations[-20:], "steps_used": self.steps,
                "user_answers": self.user_answers,
                "visible_turns": self.visible_turns[-12:],
                "input_required": self.input_required,
                "pending_question": self.last_step.message if self.last_step is not None
                and self.last_step.status == "needs_input" else None}


def signature(action):
    return json.dumps(action, sort_keys=True, ensure_ascii=False)


class AgentLoop:
    def __init__(self, registry, generate, execute, publish, progress, approval, validate,
                 max_steps=12, max_actions=24, preauthorized=None):
        self.registry, self.generate, self.execute = registry, generate, execute
        self.publish, self.progress, self.approval = publish, progress, approval
        self.validate = validate
        self.max_steps, self.max_actions = max_steps, max_actions
        self.preauthorized = preauthorized or (lambda tool, action: False)

    async def run(self, state, user_answer="", confirm=False):
        if state.status == "needs_input" and (confirm or not user_answer.strip()):
            return
        if user_answer and not confirm:
            state.user_answers.append(user_answer)
            state.input_required = False
        state.status = state.task.status = "running"
        if confirm:
            state.approved.update(signature(action) for action in state.pending_actions)
        elif state.pending_actions:
            state.status = state.task.status = "awaiting_confirmation"
            await self.approval(state.pending_actions)
            return
        if state.pending_actions:
            actions, state.pending_actions = state.pending_actions, []
            try:
                if not await self._act(state, actions):
                    return
            except ValueError as error:
                await self._stop(state, "blocked", "The approved action is no longer available or valid: " + str(error))
                return
        correction = ""
        invalid_steps = 0
        while state.steps < self.max_steps:
            state.steps += 1
            try:
                step = await self.generate(state, user_answer, correction)
                self.validate(step.actions)
                questions = [(action, self.registry.get(action['tool']).input_prompt_argument)
                             for action in step.actions
                             if self.registry.get(action['tool']).input_prompt_argument]
                if questions:
                    if len(step.actions) != 1:
                        raise ValueError('Ask one question with no other actions while waiting for the answer')
                    action, argument = questions[0]
                    prompt = action['arguments'].get(argument)
                    if not isinstance(prompt, str) or not prompt.strip():
                        raise ValueError('An input request must have a nonempty question')
                    # Legacy question tools map to the same conversation state as
                    # a native needs_input step, never a successful work action.
                    step = AgentStep('needs_input', prompt.strip())
                if state.input_required and step.status not in {"needs_input", "blocked"}:
                    raise ValueError("The tool requires a new user answer. Ask one concise question with "
                                     "status=needs_input and actions=[]; do not run tools or repeat work before an answer arrives.")
                if step.status == "complete" and state.observations and state.observations[-1]["status"] != "success":
                    raise ValueError("The latest tool result failed or needs input; resolve it or report a blocker")
            except ValueError as error:
                invalid_steps += 1
                correction = str(error)
                state.task.record("agent.step_rejected", state.owner, error=correction)
                if invalid_steps < 2:
                    continue
                await self._stop(state, "blocked", "I couldn't choose a reliable next step. " + correction)
                return
            except Exception as error:
                state.task.record("agent.generation_failed", state.owner, error=type(error).__name__)
                await self._stop(state, "blocked", "The model request failed; the task is still unfinished.")
                return
            correction = ""
            invalid_steps = 0
            state.last_step = step
            state.task.record("agent.step", state.owner, status=step.status, actions=step.actions)
            await self.publish(step.message)
            state.visible_turns.append(step.message)
            if step.status != "continue":
                state.status = state.task.status = step.status
                state.input_required = step.status == "needs_input"
                record_timing("agent_finished", status=state.status, steps=state.steps,
                              actions=state.actions_taken)
                return
            if not await self._act(state, step.actions):
                return
        await self._stop(state, "blocked", "I reached this run's step limit. The task is unfinished; I kept the verified results.")

    async def _act(self, state, actions):
        self.validate(actions)
        for index, action in enumerate(actions):
            tool = self.registry.get(action["tool"])
            key = signature(action)
            if key in state.writes_completed:
                await self._stop(state, "blocked", "That state-changing action already succeeded; I won't execute it twice.")
                return False
            if tool.risk == "write" and key not in state.approved and not self.preauthorized(tool, action):
                state.pending_actions = actions[index:]
                state.status = state.task.status = "awaiting_confirmation"
                state.task.record("approval.requested", state.owner, actions=state.pending_actions)
                await self.approval(state.pending_actions)
                return False
            previous = [o for o in state.observations if o["action"] == action]
            if previous and previous[-1]["status"] == "failed":
                retry_allowed = previous[-1]["retryable"] and len(previous) < 2
                if not retry_allowed:
                    await self._stop(state, "blocked", "Repeating the same failed action won't advance this task. "
                                     + previous[-1]["summary"])
                    return False
            if state.actions_taken >= self.max_actions:
                await self._stop(state, "blocked", "I reached this run's tool limit. The task remains unfinished.")
                return False
            state.actions_taken += 1
            state.task.record("tool.started", state.owner, tool=tool.name, arguments=action["arguments"])
            await self.progress(f"{state.owner}: running {tool.name.replace('_', ' ')}...")
            try:
                outcome = await self.execute(action)
                if not isinstance(outcome, ToolOutcome):
                    outcome = normalize_outcome(outcome)
            except Exception as error:
                transient = isinstance(error, (TimeoutError, ConnectionError)) and tool.risk != "write"
                outcome = ToolOutcome("failed", [], str(error)[:1500], type(error).__name__, transient)
            observation = {"action": action, **outcome.context()}
            state.observations.append(observation)
            if outcome.status == "needs_input":
                state.input_required = True
            state.task.record("tool.succeeded" if outcome.status == "success" else "tool.failed",
                              state.owner, tool=tool.name, result=outcome.summary,
                              error_code=outcome.error_code, retryable=outcome.retryable)
            record_timing("tool_outcome", tool=tool.name, status=outcome.status, error_code=outcome.error_code)
            if outcome.status == "success" and tool.risk == "write":
                state.writes_completed.add(key)
            await self.progress(f"{state.owner}: {tool.name.replace('_', ' ')} — {outcome.status}. "
                                + (outcome.summary[:180] if outcome.status != "success" else ""))
            # A failed prerequisite invalidates remaining actions in this batch.
            if outcome.status != "success":
                break
        return True

    async def _stop(self, state, status, message):
        state.status = state.task.status = status
        state.visible_turns.append(message)
        await self.publish(message)
        record_timing("agent_finished", status=status, steps=state.steps, actions=state.actions_taken)

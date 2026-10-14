"""A room owns its work. Characters speak and propose steps on one shared board."""
import asyncio
from dataclasses import dataclass, field
import json
import re

from services.agent_loop import signature
from services.character_diary import CHARACTERS
from services.request_timing import record as record_timing
from services.tool_outcome import ToolOutcome, normalize_outcome


@dataclass
class RoomTurn:
    status: str
    message: str
    actions: list = field(default_factory=list)
    jobs: list = field(default_factory=list)
    work_note: str = ""
    diary_updates: list = field(default_factory=list)


class RoomProtocolError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def normalize_action(value):
    """Translate only unambiguous provider tool-call envelopes; never discard extra fields."""
    if not isinstance(value, dict):
        raise RoomProtocolError("action_envelope", 'An action must be {"tool":"registered_name","arguments":{...}}')
    if set(value) == {"name", "arguments"}:
        value = {"tool": value["name"], "arguments": value["arguments"]}
    elif set(value) == {"type", "function"} and value["type"] == "function":
        function = value["function"]
        if isinstance(function, dict) and set(function) == {"name", "arguments"}:
            value = {"tool": function["name"], "arguments": function["arguments"]}
    if set(value) != {"tool", "arguments"} or not isinstance(value["tool"], str):
        raise RoomProtocolError("action_envelope", 'Each action must contain only "tool" (registered name) and "arguments" (object)')
    arguments = value["arguments"]
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise RoomProtocolError("action_arguments", "Action arguments must be an object")
    return {"tool": value["tool"], "arguments": arguments}


def parse_turn(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("Return a room turn JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    required = {"status", "message", "actions", "jobs"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"work_note", "diary_updates"}:
        raise ValueError("A room turn needs status, message, actions, jobs; work_note and diary_updates are optional")
    if not isinstance(value["status"], str) or value["status"] not in {"continue", "complete", "needs_input", "blocked"}:
        raise ValueError("Invalid room turn status")
    if not isinstance(value["message"], str) or not value["message"].strip():
        raise ValueError("Say something to the user before doing work")
    if not isinstance(value["actions"], list) or not isinstance(value["jobs"], list):
        raise ValueError("actions and jobs must be lists")
    if not isinstance(value.get("work_note", ""), str) or len(value.get("work_note", "")) > 480:
        raise ValueError("work_note must be a short public explanation, at most 480 characters")
    value["actions"] = [normalize_action(action) for action in value["actions"]]
    if value["status"] == "continue" and not (value["actions"] or value["jobs"]):
        raise ValueError("Continue needs an action or a teammate job; otherwise complete your turn")
    if value["status"] != "continue" and (value["actions"] or value["jobs"]):
        raise ValueError("Only continue may propose work")
    return RoomTurn(**value)


@dataclass
class RoomJob:
    id: str
    character: str
    instruction: str
    after: list[str] = field(default_factory=list)
    status: str = "pending"
    observations: list = field(default_factory=list)
    pending_actions: list = field(default_factory=list)
    steps: int = 0
    question: str = ""


@dataclass
class RoomState:
    task: object
    original_time: object = None
    jobs: dict[str, RoomJob] = field(default_factory=dict)
    messages: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    evidence_images: list = field(default_factory=list)
    user_answers: list = field(default_factory=list)
    approved: set = field(default_factory=set)
    writes_completed: set = field(default_factory=set)
    mutations_completed: dict = field(default_factory=dict)
    steps: int = 0
    actions_taken: int = 0
    status: str = "running"

    def context(self, job):
        return {"goal": self.task.goal, "user_answers": self.user_answers,
                "your_job": job.id, "your_instruction": job.instruction,
                "your_results": job.observations[-8:], "pending_question": job.question,
                "board": [{"id": j.id, "character": j.character, "instruction": j.instruction,
                           "after": j.after, "status": j.status} for j in self.jobs.values()],
                "room_messages": self.messages[-16:], "shared_results": self.observations[-16:]}


class SharedRoom:
    def __init__(self, registry, generate, execute, publish, progress, approval, validate,
                 max_steps=24, max_actions=24, publish_note=None):
        self.registry, self.generate, self.execute = registry, generate, execute
        self.publish, self.progress, self.approval, self.validate = publish, progress, approval, validate
        self.max_steps, self.max_actions = max_steps, max_actions
        self.publish_note = publish_note
        self._wake = asyncio.Event()
        self._mutation_lock = asyncio.Lock()
        self._reads = {}

    async def run(self, state, answer="", confirm=False):
        state.status = state.task.status = "running"
        if answer and not confirm:
            state.user_answers.append(answer)
        for job in state.jobs.values():
            if job.status == "awaiting_confirmation":
                if confirm:
                    state.approved.update(signature(a) for a in job.pending_actions)
                    job.status = "pending"
                elif answer:
                    # A changed request must be reconsidered before seeking fresh approval.
                    job.pending_actions = []
                    job.status = "pending"
            elif job.status == "needs_input" and answer:
                job.status = "pending"
        running = {}
        try:
            while True:
                self._wake.clear()
                busy = {state.jobs[key].character for key in running}
                for job in list(state.jobs.values()):
                    if job.status != "pending" or job.id in running:
                        continue
                    dependencies = [state.jobs[key] for key in job.after]
                    if any(j.status == "blocked" for j in dependencies):
                        await self._block(state, job, "A required teammate step failed; this dependent step remains unfinished.")
                    elif all(j.status == "complete" for j in dependencies) and job.character not in busy:
                        job.status = "running"
                        record_timing("room_job_started", actor=job.character, job=job.id, dependencies=len(job.after))
                        busy.add(job.character)
                        running[job.id] = asyncio.create_task(self._work(state, job))
                done = [key for key, worker in running.items() if worker.done()]
                for key in done:
                    worker = running.pop(key)
                    worker.result()
                if not running:
                    if any(j.status == "pending" and all(state.jobs[d].status == "complete" for d in j.after)
                           for j in state.jobs.values()):
                        continue
                    break
                wake = asyncio.create_task(self._wake.wait())
                try:
                    await asyncio.wait([*running.values(), wake], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    wake.cancel()
                    await asyncio.gather(wake, return_exceptions=True)
        finally:
            for worker in running.values():
                worker.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)
            reads = list(self._reads.values())
            for worker in reads:
                worker.cancel()
            await asyncio.gather(*reads, return_exceptions=True)
            self._reads.clear()
        statuses = {j.status for j in state.jobs.values()}
        state.status = state.task.status = (
            "awaiting_confirmation" if "awaiting_confirmation" in statuses else
            "needs_input" if "needs_input" in statuses else
            "blocked" if statuses - {"complete"} else "complete")
        pending = [a for j in state.jobs.values() for a in j.pending_actions]
        if pending:
            await self.approval(list({signature(a): a for a in pending}.values()))
        record_timing("room_finished", status=state.status, steps=state.steps, actions=state.actions_taken)

    def _check_jobs(self, state, job, proposals):
        if len(state.jobs) + len(proposals) > self.max_steps:
            raise ValueError("The room work board is full")
        proposed = {}
        for item in proposals:
            if not isinstance(item, dict) or set(item) != {"id", "character", "instruction", "after"}:
                raise ValueError("Each job needs id, character, instruction, after")
            key = item["id"]
            if not isinstance(key, str) or not key.strip() or len(key) > 80 or key in state.jobs or key in proposed:
                raise ValueError("Job IDs must be unique short strings")
            if item["character"] not in CHARACTERS or not isinstance(item["instruction"], str) or not item["instruction"].strip():
                raise ValueError("Choose a room character and a concrete instruction")
            if len(item["instruction"]) > 2000 or not isinstance(item["after"], list) or any(not isinstance(d, str) for d in item["after"]):
                raise ValueError("Invalid job instruction or dependencies")
            proposed[key] = RoomJob(**item)
        graph = {key: j.after for key, j in {**state.jobs, **proposed}.items()}
        def visit(key, path):
            if key not in graph or key in path:
                raise ValueError("Job dependencies must exist and cannot form a cycle")
            for dependency in graph[key]:
                visit(dependency, path | {key})
        for key in proposed:
            visit(key, set())
        return proposed

    async def _work(self, state, job):
        correction = ""
        invalid = 0
        try:
            if job.pending_actions:
                actions, job.pending_actions = job.pending_actions, []
                if not await self._act(state, job, actions):
                    return
            while True:
                if state.steps >= self.max_steps:
                    await self._block(state, job, "The room reached its step limit. Verified results are kept; this step is unfinished.")
                    return
                state.steps += 1
                job.steps += 1
                turn = None
                try:
                    # No mutable state is exposed to an in-flight model request.
                    context = json.loads(json.dumps(state.context(job), ensure_ascii=False))
                    turn = await self.generate(job, context, correction)
                    self.validate(turn.actions)
                    proposals = self._check_jobs(state, job, turn.jobs)
                    if turn.status == "complete" and job.observations and job.observations[-1]["status"] != "success":
                        raise ValueError("Your latest tool failed or needs input; recover or report blocked/needs_input")
                except ValueError as error:
                    correction = str(error)
                    if turn is not None:
                        correction += "\nRejected actions (none ran): " + json.dumps(turn.actions, ensure_ascii=False)[:4000]
                    correction += '\nEach action has tool and arguments; arguments is a JSON-encoded object string. Return the full corrected turn.'
                    invalid += 1
                    state.task.record("room.turn_rejected", job.character, job=job.id, error=correction)
                    record_timing("room_turn_rejected", actor=job.character, job=job.id,
                                  phase="validation" if turn is not None else "generation_or_parse", attempt=invalid,
                                  error=type(error).__name__, code=getattr(error, "code", "invalid_turn"))
                    if invalid < 2:
                        continue
                    await self._block(state, job, self._failure_message(state, "invalid_turn"))
                    return
                invalid, correction = 0, ""
                # Commit before the first await so concurrently completed turns see unique IDs.
                state.jobs.update(proposals)
                for added in proposals.values():
                    state.task.record("room.job_added", job.character, job=added.id,
                                      character=added.character, instruction=added.instruction, after=added.after)
                await self._say(state, job, turn.message, turn.work_note)
                self._wake.set()
                if turn.status != "continue":
                    job.status = turn.status
                    if turn.status == "needs_input":
                        job.question = turn.message
                    return
                if not turn.actions:
                    # Delegating ends this turn; the room still owns all outstanding jobs.
                    job.status = "complete"
                    return
                if not await self._act(state, job, turn.actions):
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state.task.record("room.job_failed", job.character, job=job.id, error=type(error).__name__)
            record_timing("room_job_failed", actor=job.character, job=job.id, error=type(error).__name__)
            await self._block(state, job, self._failure_message(state, "request_failed"))
        finally:
            record_timing("room_job_finished", actor=job.character, job=job.id, status=job.status)
            self._wake.set()

    @staticmethod
    def _failure_message(state, reason):
        latest = state.user_answers[-1] if state.user_answers else state.task.goal
        chinese = bool(re.search(r"[\u4e00-\u9fff]", latest))
        if reason == "invalid_turn":
            return ("这一步我没能可靠地启动，操作还没有完成。" if chinese else
                    "I couldn't start this step reliably; the operation is still unfinished.")
        return ("我这边的请求失败了，这一步还没完成；其他成员可以继续各自的工作。" if chinese else
                "My request failed; this step is unfinished. The other characters can continue.")

    async def _say(self, state, job, message, work_note=""):
        entry = {"character": job.character, "job": job.id, "message": message}
        if work_note:
            entry["work_note"] = work_note
        state.messages.append(entry)
        state.task.record("room.message", job.character, job=job.id, message=message, work_note=work_note)
        if work_note and self.publish_note is not None:
            await self.publish_note(job.character, work_note)
        await self.publish(job.character, message)

    async def _block(self, state, job, message):
        job.status = "blocked"
        await self._say(state, job, message)

    async def _act(self, state, job, actions):
        self.validate(actions)
        for index, action in enumerate(actions):
            tool = self.registry.get(action["tool"])
            key = signature(action)
            if tool.risk == "write" and key not in state.approved:
                job.pending_actions = actions[index:]
                job.status = "awaiting_confirmation"
                state.task.record("approval.requested", job.character, job=job.id, actions=job.pending_actions)
                return False
            if tool.risk == "read_only":
                if key not in self._reads:
                    self._reads[key] = asyncio.create_task(self._execute(state, job, action, tool))
                running = self._reads[key]
                try:
                    outcome = await asyncio.shield(running)
                finally:
                    if running.done() and self._reads.get(key) is running:
                        self._reads.pop(key, None)
            else:
                async with self._mutation_lock:
                    if key in state.mutations_completed:
                        previous = state.mutations_completed[key]
                        outcome = ToolOutcome("success", [], "This exact action already succeeded; it was not repeated.\n" + previous.summary,
                                              sources=previous.sources)
                    else:
                        outcome = await self._execute(state, job, action, tool)
            observation = {"job": job.id, "character": job.character, "action": action, **outcome.context()}
            job.observations.append(observation)
            state.observations.append(observation)
            self._wake.set()
            if outcome.status != "success":
                break  # A failed prerequisite invalidates the rest of this batch.
        return True

    async def _execute(self, state, job, action, tool):
        previous = [o for o in state.observations if o["action"] == action]
        if previous and previous[-1]["status"] == "failed" and not (previous[-1]["retryable"] and len(previous) < 2):
            return ToolOutcome("failed", [], "An identical non-retryable action already failed. " + previous[-1]["summary"], "repeated_failure")
        if state.actions_taken >= self.max_actions:
            return ToolOutcome("failed", [], "The room reached its tool limit; the goal is unfinished.", "action_limit")
        state.actions_taken += 1
        state.task.record("tool.started", job.character, job=job.id, tool=tool.name, arguments=action["arguments"])
        await self.progress(f"{job.character}: running {tool.name.replace('_', ' ')}...")
        try:
            outcome = normalize_outcome(await self.execute(job.character, action))
        except Exception as error:
            transient = isinstance(error, (TimeoutError, ConnectionError)) and tool.risk == "read_only"
            outcome = ToolOutcome("failed", [], str(error)[:1500], type(error).__name__, transient)
        if outcome.status == "success" and tool.risk == "write":
            state.writes_completed.add(signature(action))
        if outcome.status == "success" and tool.risk != "read_only":
            state.mutations_completed[signature(action)] = outcome
        state.task.record("tool.succeeded" if outcome.status == "success" else "tool.failed", job.character,
                          job=job.id, tool=tool.name, result=outcome.summary, error_code=outcome.error_code)
        record_timing("tool_outcome", actor=job.character, tool=tool.name, status=outcome.status,
                      error_code=outcome.error_code, retryable=outcome.retryable)
        await self.progress(f"{job.character}: {tool.name.replace('_', ' ')} — {outcome.status}. "
                            + (outcome.summary[:180] if outcome.status != "success" else ""))
        return outcome

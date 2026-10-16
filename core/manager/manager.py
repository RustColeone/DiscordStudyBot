"""The Manager: wiring and policy. It owns registries, durable runs, approvals and the tank.

It contains no feature logic; capabilities and engines are opaque objects registered through slots.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import secrets
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Protocol

import jsonschema

from contracts import (Address, Capability, Delivery, Display, Engine, FileRef, ModelError, ModelRequest,
                       ModelResult, Operation, Persona, Reply, Request, Risk, RunYield, Sender, ToolResult)
from core.audit import Audit
from core.manager.handles import SessionHandle, ToolContextHandle
from core.manager.policy import Assignment, Owners, most_specific
from core.providers.registry import Providers
from core.serialize import request_from_dict, request_to_dict
from core.storage.tank import RunRecord, Tank

COMMAND_ENGINE = "$command"
CORE_COMMANDS = ("help", "approve", "deny", "runs", "cancel", "tools", "call")


class GatewayLink(Protocol):
    async def deliver(self, reply: Reply) -> Delivery: ...

    async def operate(self, interface: str, operation: str, arguments: Mapping[str, Any]) -> ToolResult: ...

    def interface_operations(self) -> Mapping[str, tuple[Operation, ...]]: ...

    def admin(self) -> Any: ...

    def schedule(self, address: Address, at: float, text: str, sender: Sender) -> str: ...

    def scheduled_for(self, address: Address) -> list[dict]: ...

    def cancel_scheduled(self, address: Address, scheduled_id: str) -> bool: ...


def signature(name: str, arguments: Mapping[str, Any]) -> str:
    body = json.dumps({"name": name, "arguments": arguments}, sort_keys=True, ensure_ascii=False, default=str)
    return "sig:" + hashlib.sha256(body.encode()).hexdigest()[:24]


class Manager:
    def __init__(self, tank: Tank, providers: Providers, *, personas: Mapping[str, Persona],
                 assignments: Mapping[str, Assignment], owners: Owners, control: Address | None,
                 data_dir: str | Path, audit: Audit | None = None):
        self.tank, self.providers = tank, providers
        self.personas = dict(personas)
        self._config_assignments = dict(assignments)
        self._overrides = tank.store("core:assignments")
        self.owners = owners
        self._control_config = control
        self._control_reported: Address | None = None
        self.data_dir = Path(data_dir)
        self.audit = audit or Audit()
        self.engines: dict[str, Engine] = {}
        self.capabilities: dict[str, Capability] = {}
        self._origin: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._approval_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.gateway: GatewayLink | None = None
        self._stopping = False
        self._delivery_seconds: dict[str, float] = defaultdict(float)
        self._delivering: dict[str, float] = {}
        # Runs currently waiting on a capability or a delivery. They do not hold their lane:
        # new messages start new runs instead of queuing behind long work (a video conversion).
        self._working: dict[str, list[tuple[str, float]]] = {}
        self._side_tasks: set[asyncio.Task] = set()

    @property
    def control(self) -> Address | None:
        return self._control_config or self._control_reported

    def report_owners(self, interface: str, ids, control: Address | None) -> None:
        self.owners.report(interface, ids)
        if control is not None:
            self._control_reported = control
        self.audit.record("owners_reported", interface=interface, count=len(list(ids)),
                          control=str(self.control) if self.control else None)

    # ---- registration -------------------------------------------------------------------------
    @property
    def assignments(self) -> dict[str, Assignment]:
        merged = dict(self._config_assignments)
        for pattern in self._overrides.keys():
            merged[pattern] = Assignment.from_dict(self._overrides.get(pattern))
        return merged

    def register_persona(self, persona: Persona, extension: str) -> None:
        # Configuration wins over an extension's default persona of the same name.
        self.personas.setdefault(persona.name, persona)

    def set_assignment(self, pattern: str, value: Mapping[str, Any]) -> Assignment:
        if pattern != "*" and ":" not in pattern:
            raise ValueError("Patterns are '*', 'interface:*' or 'interface:conversation'")
        assignment = Assignment.from_config(pattern, {**dict(value), "since": time.time()})
        engine = self.engines.get(assignment.engine)
        if engine is None:
            raise ValueError(f"Engine {assignment.engine!r} is not registered")
        missing = [name for name in assignment.personas if name not in self.personas]
        if missing or not assignment.personas or (engine.spec.personas == "one" and len(assignment.personas) != 1):
            raise ValueError(f"Engine {engine.spec.name!r} needs {'exactly one persona' if engine.spec.personas == 'one' else 'personas'}"
                             + (f"; unknown {missing}" if missing else ""))
        self._overrides.set(pattern, assignment.to_dict())
        self.audit.record("assignment_set", pattern=pattern)
        self._sweep_disabled()
        return assignment

    def delete_assignment(self, pattern: str) -> None:
        if pattern not in self._overrides.keys():
            raise ValueError(f"{pattern!r} has no runtime assignment (configured ones are changed in core.yml)")
        self._overrides.delete(pattern)
        self._sweep_disabled()

    def _sweep_disabled(self) -> None:
        """Turning a conversation off also cancels its open work and expires its pending approvals."""
        for record in self.tank.runs(("running", "waiting_input", "waiting_approval"), limit=1000):
            if record.engine == COMMAND_ENGINE:
                continue
            assignment = self.assignment_for(Address.parse(record.address))
            if assignment is None or not assignment.enabled:
                self._spawn(self._cancel_record(record, "conversation_disabled"))

    def _spawn(self, coroutine) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coroutine)
        except RuntimeError:
            coroutine.close()
            return
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def register_engine(self, engine: Engine, extension: str) -> None:
        name = engine.spec.name
        if name in self.engines or name == COMMAND_ENGINE:
            raise ValueError(f"Engine {name!r} is already registered")
        self.engines[name] = engine
        self._origin["engine:" + name] = extension

    def register_capability(self, capability: Capability, extension: str) -> None:
        if capability.name in self.capabilities:
            raise ValueError(f"Capability {capability.name!r} is already registered")
        if capability.extension != extension:
            raise ValueError(f"Capability {capability.name!r} must use its extension's namespace {extension!r}")
        if capability.command and (capability.command in CORE_COMMANDS or any(
                other.command == capability.command for other in self.capabilities.values())):
            raise ValueError(f"Command ${capability.command} is already taken")
        self.capabilities[capability.name] = capability

    def attach_gateway(self, gateway: GatewayLink) -> None:
        self.gateway = gateway
        from core.manager.core_capabilities import core_capabilities
        for capability in core_capabilities(self, gateway):
            self.register_capability(capability, capability.extension)

    def problems(self) -> list[str]:
        problems = []
        for pattern, assignment in self.assignments.items():
            engine = self.engines.get(assignment.engine)
            if engine is None:
                problems.append(f"assignment {pattern!r}: engine {assignment.engine!r} is not registered")
            missing = [name for name in assignment.personas if name not in self.personas]
            if missing:
                problems.append(f"assignment {pattern!r}: unknown personas {missing}")
            if engine is not None and engine.spec.personas == "one" and len(assignment.personas) != 1:
                problems.append(f"assignment {pattern!r}: engine {engine.spec.name!r} needs exactly one persona")
            if engine is not None and engine.spec.personas == "many" and not assignment.personas:
                problems.append(f"assignment {pattern!r}: engine {engine.spec.name!r} needs personas")
        return problems

    # ---- lifecycle --------------------------------------------------------------------------------
    async def start(self) -> None:
        self._stopping = False
        self._clean_workspaces()
        for record in self.tank.runs(("running",), limit=1000):
            if record.engine == COMMAND_ENGINE:
                self.tank.update_run(record.id, status="failed", error="interrupted")
                continue
            self.tank.append_event(record.id, "resumed", {})
            self.audit.record("run_resumed", run=record.id, engine=record.engine)
            self._launch(record.id, resumed=True)

    def _clean_workspaces(self, keep_days: float = 7.0) -> None:
        root = self.data_dir / "workspaces"
        if not root.is_dir():
            return
        import shutil
        cutoff = time.time() - keep_days * 86400
        for directory in root.iterdir():
            record = self.tank.run(directory.name)
            if record is None or (record.status in ("done", "failed", "cancelled") and record.updated_at < cutoff):
                shutil.rmtree(directory, ignore_errors=True)

    async def stop(self) -> None:
        self._stopping = True
        for task in list(self._side_tasks):
            task.cancel()
        await asyncio.gather(*self._side_tasks, return_exceptions=True)
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def idle(self) -> None:
        """Wait until no run task is executing (used by tests and shutdown)."""
        while self._tasks or self._side_tasks:
            await asyncio.gather(*list(self._tasks.values()), *list(self._side_tasks), return_exceptions=True)

    # ---- inbound ----------------------------------------------------------------------------------
    def is_owner(self, request: Request) -> bool:
        return self.owners.is_owner(request.address.interface, request.sender)

    def assignment_for(self, address: Address) -> Assignment | None:
        return most_specific(self.assignments, address)[1]

    async def accept(self, request: Request) -> None:
        """Called by the gateway for an admitted conversation message, in order per conversation."""
        address = str(request.address)
        async with self._locks[address]:
            self.tank.record_message(address, "in", request.text, kind=request.kind, sender_id=request.sender.id,
                                     sender_name=request.sender.name, message_id=request.id,
                                     reply_to=request.reply_to, quoted_text=request.quoted_text,
                                     at=request.received_at)
            assignment = self.assignment_for(request.address)
            if assignment is None or not assignment.enabled or request.received_at < assignment.since:
                self.audit.record("not_assigned", address=address,
                                  reason="none" if assignment is None else "disabled" if not assignment.enabled
                                  else "before_assignment")
                return
            for active in self.tank.lane_runs(assignment.lane_key(request)):
                if self._working.get(active.id):
                    continue
                if active.status == "running":
                    self.tank.append_event(active.id, "inbox", {"request": request_to_dict(request)})
                    self.audit.record("run_inbox", run=active.id)
                else:
                    self.tank.append_event(active.id, "input", {"request": request_to_dict(request)})
                    self.tank.update_run(active.id, status="running")
                    self._launch(active.id, resumed=False)
                return
            self._create_run(request)

    def _work(self, run_id: str, what: str):
        manager = self

        class _Working:
            def __enter__(self):
                manager._working.setdefault(run_id, []).append((what, time.time()))

            def __exit__(self, *error):
                stack = manager._working.get(run_id, [])
                if stack:
                    stack.pop()
                if not stack:
                    manager._working.pop(run_id, None)
                return False
        return _Working()

    def activity(self, run_id: str) -> list[dict[str, Any]]:
        """Other open runs in the same conversation, for engines to see work already in progress."""
        record = self.tank.run(run_id)
        result = []
        for other in self.tank.runs(("running", "waiting_input", "waiting_approval"), address=record.address):
            if other.id == run_id or other.engine == COMMAND_ENGINE:
                continue
            trigger = request_from_dict(other.trigger)
            working = self._working.get(other.id)
            result.append({"run_id": other.id, "status": "working" if working else other.status,
                           "sender_id": trigger.sender.id, "sender_name": trigger.sender.name,
                           "request": trigger.text[:300], "doing": working[-1][0] if working else "",
                           "since": working[-1][1] if working else other.updated_at,
                           "started_at": other.created_at})
        return result

    def _create_run(self, request: Request, inbox: list[Request] = ()) -> str | None:
        assignment = self.assignment_for(request.address)
        if assignment is None or not assignment.enabled:
            self.audit.record("not_assigned", address=str(request.address), reason="leftover")
            return None
        record = RunRecord(id=uuid.uuid4().hex[:12], address=str(request.address),
                           lane=assignment.lane_key(request), engine=assignment.engine,
                           personas=list(assignment.personas), assignment=assignment.to_dict(), status="running",
                           origin=request.origin, trigger=request_to_dict(request),
                           usage={"model_calls": 0, "tool_calls": 0, "seconds": 0.0,
                                  "input_tokens": 0, "output_tokens": 0})
        self.tank.create_run(record)
        self.tank.append_event(record.id, "input", {"request": request_to_dict(request)})
        for item in inbox:
            self.tank.append_event(record.id, "inbox", {"request": request_to_dict(item)})
        self.audit.record("run_started", run=record.id, address=record.address, engine=record.engine,
                          origin=record.origin)
        self._launch(record.id, resumed=False)
        return record.id

    def _launch(self, run_id: str, resumed: bool) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(self._drive(run_id, resumed), name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda done, run_id=run_id: self._tasks.pop(run_id, None)
                               if self._tasks.get(run_id) is done else None)

    # ---- driving runs -----------------------------------------------------------------------------
    async def _drive(self, run_id: str, resumed: bool) -> None:
        record = self.tank.run(run_id)
        if record is None or record.status != "running":
            return
        engine = self.engines.get(record.engine)
        assignment = Assignment.from_dict(record.assignment)
        failure: tuple[str, str] | None = None
        outcome: Any = None
        started_events = len(self.tank.events(run_id))
        remaining = assignment.budget.seconds - float(record.usage.get("seconds", 0))
        started = time.monotonic()
        if engine is None:
            failure = ("engine_missing", f"Engine {record.engine!r} is not registered")
        elif remaining <= 0:
            failure = ("budget_exceeded", "The run's time budget is used up")
        else:
            events = self.tank.events(run_id, ("input", "tool_result", "approval_denied", "resumed"))
            session = SessionHandle(self, record, events, resumed)
            self._delivery_seconds[run_id] = 0.0
            try:
                outcome = await self._watch(run_id, engine.run(session), started, remaining)
            except asyncio.CancelledError:
                self._add_usage(run_id, seconds=self._active_seconds(run_id, started))
                raise
            except asyncio.TimeoutError:
                failure = ("budget_exceeded", "The run's time budget is used up")
            except ModelError as error:
                failure = (error.kind, str(error))
            except Exception as error:
                failure = ("engine_error", f"{type(error).__name__}: {error}"[:500])
        self._add_usage(run_id, seconds=self._active_seconds(run_id, started))
        self._delivery_seconds.pop(run_id, None)
        address = record.address
        async with self._locks[address]:
            record = self.tank.run(run_id)
            if record is None or record.status != "running":
                return
            if failure is None:
                try:
                    outcome = RunYield(outcome)
                except ValueError:
                    failure = ("engine_protocol", f"Engine returned {outcome!r} instead of a RunYield")
            if failure is not None:
                await self._fail(record, *failure)
            elif outcome is RunYield.WAIT_APPROVAL:
                if self.tank.approvals("pending", run_id):
                    self.tank.update_run(run_id, status="waiting_approval")
                elif any(event.kind in ("tool_result", "approval_denied")
                         for event in self.tank.events(run_id, after=started_events - 1)):
                    self._launch(run_id, resumed=False)
                    return
                else:
                    await self._fail(record, "engine_protocol", "Engine waited for approval without a pending approval")
            elif outcome is RunYield.WAIT_INPUT:
                pending = self.take_inbox(run_id)
                if pending:
                    for request in pending:
                        self.tank.append_event(run_id, "input", {"request": request_to_dict(request)})
                    self._launch(run_id, resumed=False)
                    return
                self.tank.update_run(run_id, status="waiting_input")
            else:
                self.tank.update_run(run_id, status="done")
            self.audit.record("run_yielded", run=run_id, status=self.tank.run(run_id).status)
            final = self.tank.run(run_id).status
            if final in ("done", "failed"):
                leftover = self.take_inbox(run_id)
                if leftover:
                    self._create_run(leftover[0], leftover[1:])

    def _active_seconds(self, run_id: str, started: float) -> float:
        # Waiting for a platform to deliver (for example a ten-minute upload) is not the run's work time.
        now = time.monotonic()
        delivering = self._delivering.get(run_id)
        ongoing = now - delivering if delivering is not None else 0.0
        return max(0.0, now - started - self._delivery_seconds.get(run_id, 0.0) - ongoing)

    async def _watch(self, run_id: str, coroutine, started: float, remaining: float):
        task = asyncio.ensure_future(coroutine)
        try:
            while True:
                left = remaining - self._active_seconds(run_id, started)
                if left <= 0:
                    raise asyncio.TimeoutError()
                done, _ = await asyncio.wait({task}, timeout=min(left, 5.0))
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _fail(self, record: RunRecord, code: str, detail: str) -> None:
        self.tank.update_run(record.id, status="failed", error=f"{code}: {detail}"[:1000])
        for approval in self.tank.approvals("pending", record.id):
            self.tank.decide_approval(approval["id"], "expired", "core")
        self.audit.record("run_failed", run=record.id, code=code)
        trigger = request_from_dict(record.trigger)
        await self._deliver(Reply(trigger.address, "notice", code="run_failed", in_reply_to=trigger.id,
                                  run_id=record.id, data={"error_code": code}))
        if self.control is not None and self.control != trigger.address:
            await self._deliver(Reply(self.control, "notice", code="run_failed_detail", run_id=record.id,
                                      text=f"Run {record.id} in {record.address} failed ({code}): {detail}",
                                      data={"error_code": code, "address": record.address}))

    def _add_usage(self, run_id: str, **increments: float) -> dict:
        record = self.tank.run(run_id)
        if record is None:
            return {}
        usage = dict(record.usage)
        for key, value in increments.items():
            usage[key] = round(usage.get(key, 0) + value, 3)
        self.tank.update_run(run_id, usage=usage)
        return usage

    def save_state(self, run_id: str, state: dict) -> None:
        json.dumps(state)  # engine state must be JSON
        self.tank.update_run(run_id, state=state)

    def take_inbox(self, run_id: str) -> list[Request]:
        record = self.tank.run(run_id)
        events = self.tank.events(run_id, ("inbox",), after=record.inbox_cursor - 1 if record.inbox_cursor else -1)
        events = [event for event in events if event.index >= record.inbox_cursor]
        if not events:
            return []
        self.tank.update_run(run_id, inbox_cursor=events[-1].index + 1)
        return [request_from_dict(event.data["request"]) for event in events]

    # ---- models -----------------------------------------------------------------------------------
    async def generate(self, run_id: str, request: ModelRequest, profile: str) -> ModelResult:
        record = self.tank.run(run_id)
        budget = Assignment.from_dict(record.assignment).budget
        if record.usage.get("model_calls", 0) >= budget.model_calls:
            raise ModelError("budget_exceeded", "This run used its model-call budget")
        self._add_usage(run_id, model_calls=1)
        result = await self.providers.generate(request, profile)
        self._add_usage(run_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens)
        self.audit.record("model_call", run=run_id, profile=profile, provider=result.provider, model=result.model,
                          duration_s=result.duration_s, input_tokens=result.input_tokens,
                          output_tokens=result.output_tokens)
        return result

    # ---- tools ------------------------------------------------------------------------------------
    def _caller_is_owner(self, record: RunRecord, assignment: Assignment) -> bool:
        trigger = request_from_dict(record.trigger)
        if trigger.origin == "routine":
            return assignment.trusted
        return self.owners.is_owner(trigger.address.interface, trigger.sender)

    def _refusal(self, record: RunRecord, capability: Capability | None, name: str) -> ToolResult | None:
        if capability is None:
            return ToolResult.fail(f"No capability named {name!r} exists", "unknown_tool")
        assignment = Assignment.from_dict(record.assignment)
        if not assignment.allows(capability.name):
            return ToolResult.fail(f"{capability.name} is not allowed in this conversation", "not_allowed")
        if capability.owner_only and not self._caller_is_owner(record, assignment):
            return ToolResult.fail(f"{capability.name} is available only to the owner", "owner_only")
        display = request_from_dict(record.trigger).display
        missing = set(capability.needs) - display.features()
        if missing:
            return ToolResult.fail(f"{capability.name} needs {sorted(missing)}, which this conversation lacks",
                                   "unsupported_here")
        return None

    def tool_schemas(self, run_id: str) -> list[dict[str, Any]]:
        record = self.tank.run(run_id)
        return [capability.schema() for capability in sorted(self.capabilities.values(), key=lambda c: c.name)
                if not capability.internal and self._refusal(record, capability, capability.name) is None]

    async def call_tool(self, run_id: str, name: str, arguments: dict, call_id: str | None = None, *,
                        approved: bool = False, key: str | None = None) -> ToolResult:
        record = self.tank.run(run_id)
        capability = self.capabilities.get(name)
        refusal = self._refusal(record, capability, name)
        if refusal is not None:
            self.audit.record("tool_refused", run=run_id, tool=name, code=refusal.error_code)
            return refusal
        try:
            jsonschema.validate(arguments, capability.schema()["parameters"])
        except jsonschema.ValidationError as error:
            return ToolResult.fail(f"Invalid arguments for {name}: {error.message}", "invalid_arguments")
        key = key or (f"call:{call_id}" if call_id else signature(name, arguments))
        prior = self.tank.action(run_id, key)
        if prior is not None:
            if prior["name"] != name or prior["arguments"] != arguments:
                return ToolResult.fail("This call_id was already used for a different call", "call_id_conflict")
            if prior["status"] == "done":
                if call_id or capability.risk is not Risk.READ:
                    result = ToolResult.from_dict(prior["result"])
                    if call_id is None:
                        result = ToolResult(result.status, "This exact action already ran in this run and was "
                                            "not repeated.\n" + result.summary, result.files, result.sources,
                                            {**result.data, "already_completed": True}, result.error_code,
                                            result.retryable)
                    return result
            elif capability.risk is not Risk.READ:
                result = ToolResult.fail("An earlier attempt of this action was interrupted by a restart and may "
                                         "have taken effect; it was not repeated.", "uncertain_previous_attempt")
                self.tank.finish_action(run_id, key, result.to_dict())
                return result
        assignment = Assignment.from_dict(record.assignment)
        if capability.risk is Risk.WRITE and not approved and not assignment.preapproves(name):
            return await self._request_approval(record, capability, arguments, key)
        if record.usage.get("tool_calls", 0) >= assignment.budget.tool_calls:
            return ToolResult.fail("This run used its tool-call budget", "budget_exceeded")
        self._add_usage(run_id, tool_calls=1)
        self.tank.start_action(run_id, key, name, arguments, capability.risk.value)
        workspace = self.data_dir / "workspaces" / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        context = ToolContextHandle(self, record, capability.extension, workspace)
        started = time.monotonic()
        try:
            with self._work(run_id, name):
                result = await asyncio.wait_for(capability.run(arguments, context), timeout=capability.timeout_s)
            if not isinstance(result, ToolResult):
                result = ToolResult.fail(f"{name} returned {type(result).__name__} instead of a ToolResult",
                                         "invalid_result")
        except asyncio.CancelledError:
            raise
        except ModelError as error:
            result = ToolResult.fail(str(error), error.kind)
        except Exception as error:
            transient = isinstance(error, (TimeoutError, ConnectionError, asyncio.TimeoutError))
            result = ToolResult.fail(f"{name} failed: {type(error).__name__}: {error}"[:1500], type(error).__name__,
                                     retryable=transient and capability.risk is Risk.READ)
        self.tank.finish_action(run_id, key, result.to_dict())
        self.audit.record("tool_call", run=run_id, tool=name, risk=capability.risk.value, status=result.status,
                          code=result.error_code, duration_s=round(time.monotonic() - started, 3))
        return result

    async def _request_approval(self, record: RunRecord, capability: Capability, arguments: dict,
                                key: str) -> ToolResult:
        for approval in self.tank.approvals("pending", record.id):
            if approval["call_key"] == key:
                return ToolResult("needs_approval", f"Waiting for owner approval {approval['id']}.",
                                  data={"approval_id": approval["id"]})
        trigger = request_from_dict(record.trigger)
        route = self.control
        if route is None and self.owners.is_owner(trigger.address.interface, trigger.sender):
            route = trigger.address
        if route is None:
            return ToolResult.fail("No owner approval route is configured, so this write cannot run",
                                   "no_approval_route")
        approval_id = secrets.token_hex(3)
        self.tank.create_approval(approval_id, record.id, key, capability.name, arguments, record.address)
        self.audit.record("approval_requested", run=record.id, approval=approval_id, tool=capability.name)
        await self._deliver(Reply(
            route, "notice", code="approval_request", run_id=record.id,
            text=(f"Approval {approval_id}: {capability.name} {json.dumps(arguments, ensure_ascii=False)}\n"
                  f"Requested in {record.address} (run {record.id}). Reply $approve {approval_id} or "
                  f"$deny {approval_id}."),
            data={"approval_id": approval_id, "capability": capability.name, "address": record.address,
                  "arguments": json.dumps(arguments, ensure_ascii=False)}))
        return ToolResult("needs_approval", f"Waiting for owner approval {approval_id}.",
                          data={"approval_id": approval_id})

    async def decide(self, approval_id: str, approve: bool, decided_by: Request) -> str:
        if not self.is_owner(decided_by):
            return "Only the owner can decide approvals."
        async with self._approval_locks[approval_id]:
            approval = self.tank.approval(approval_id)
            if approval is None or approval["status"] != "pending":
                return f"Approval {approval_id} is not pending."
            record = self.tank.run(approval["run_id"])
            assignment = self.assignment_for(Address.parse(approval["address"]))
            if (record is None or record.status not in ("running", "waiting_approval")
                    or (record.engine != COMMAND_ENGINE and (assignment is None or not assignment.enabled))):
                self.tank.decide_approval(approval_id, "expired", str(decided_by.address))
                return f"Approval {approval_id} expired; its run or conversation is no longer active."
            self.tank.decide_approval(approval_id, "approved" if approve else "denied", str(decided_by.address))
            call = {"call_key": approval["call_key"], "name": approval["name"], "arguments": approval["arguments"]}
            if approve:
                result = await self.call_tool(record.id, approval["name"], approval["arguments"],
                                              approved=True, key=approval["call_key"])
                self.tank.append_event(record.id, "tool_result", {**call, "result": result.to_dict()})
                summary = f"Approved {approval_id}: {approval['name']} → {result.status}."
            else:
                result = None
                self.tank.append_event(record.id, "approval_denied", call)
                summary = f"Denied {approval_id}: {approval['name']} was not run."
            self.audit.record("approval_decided", approval=approval_id, approved=approve)
        await self._resume_after_decision(record.id, result)
        return summary

    async def _resume_after_decision(self, run_id: str, result: ToolResult | None) -> None:
        record = self.tank.run(run_id)
        async with self._locks[record.address]:
            record = self.tank.run(run_id)
            if record.status != "waiting_approval" or self.tank.approvals("pending", run_id):
                return
            if record.engine == COMMAND_ENGINE:
                self.tank.update_run(run_id, status="done")
            else:
                self.tank.update_run(run_id, status="running")
                self._launch(run_id, resumed=False)
                return
        if result is not None:
            await self._deliver_result(record, result)

    # ---- outbound ---------------------------------------------------------------------------------
    async def _deliver(self, reply: Reply) -> Delivery:
        if self.gateway is None:
            return Delivery(False, detail="no gateway")
        try:
            delivery = await self.gateway.deliver(reply)
        except Exception as error:
            delivery = Delivery(False, detail=f"{type(error).__name__}: {error}", uncertain=True)
        self.audit.record("delivery", address=str(reply.address), kind=reply.kind, code=reply.code,
                          ok=delivery.ok, uncertain=delivery.uncertain, run=reply.run_id)
        return delivery

    def _closed(self, record: RunRecord) -> str | None:
        """Why a run's own output must not be sent now (re-checked right before every delivery)."""
        if record.engine == COMMAND_ENGINE:
            return None
        if record.status != "running":
            return "run_closed"
        assignment = self.assignment_for(Address.parse(record.address))
        if assignment is None or not assignment.enabled:
            return "conversation_disabled"
        return None

    async def _run_output(self, record: RunRecord, reply: Reply) -> Delivery:
        reason = self._closed(record)
        if reason is not None:
            self.audit.record("delivery_refused", run=record.id, reason=reason)
            return Delivery(False, detail=reason)
        started = time.monotonic()
        nested = record.id in self._delivering
        if not nested:
            self._delivering[record.id] = started
        try:
            if reply.kind == "file":
                with self._work(record.id, "delivering a file"):
                    return await self._deliver(reply)
            return await self._deliver(reply)
        finally:
            if not nested:
                self._delivering.pop(record.id, None)
                self._delivery_seconds[record.id] = (self._delivery_seconds.get(record.id, 0.0)
                                                     + time.monotonic() - started)

    async def reply(self, run_id: str, text: str, quote: str | None = None) -> Delivery:
        record = self.tank.run(run_id)
        trigger = request_from_dict(record.trigger)
        if not text.strip():
            return Delivery(False, detail="empty")
        source = "command" if record.engine == COMMAND_ENGINE else "run"
        delivery = await self._run_output(record, Reply(trigger.address, "text", text=text,
                                                        quote=quote if trigger.display.quotes else None,
                                                        in_reply_to=trigger.id, run_id=run_id, source=source))
        if delivery.ok:
            self.tank.record_message(record.address, "out", text, message_id=delivery.message_id, run_id=run_id,
                                     reply_to=trigger.id)
        return delivery

    async def send_file(self, run_id: str, file: FileRef, caption: str = "") -> Delivery:
        record = self.tank.run(run_id)
        trigger = request_from_dict(record.trigger)
        display: Display = trigger.display
        path = Path(file.path)
        if not display.files:
            return Delivery(False, detail="unsupported")
        if not path.is_file():
            return Delivery(False, detail="missing_file")
        size = path.stat().st_size
        if size > display.max_upload_mb * 1024 * 1024:
            return Delivery(False, detail="too_large")
        file = FileRef(str(path), file.name or path.name, file.mime, size)
        source = "command" if record.engine == COMMAND_ENGINE else "run"
        delivery = await self._run_output(record, Reply(trigger.address, "file", text=caption, file=file,
                                                        in_reply_to=trigger.id, run_id=run_id, source=source))
        if delivery.ok:
            self.tank.record_message(record.address, "out", caption or file.name, kind="file",
                                     message_id=delivery.message_id, run_id=run_id, reply_to=trigger.id)
        return delivery

    async def progress(self, run_id: str, text: str) -> None:
        record = self.tank.run(run_id)
        trigger = request_from_dict(record.trigger)
        await self._run_output(record, Reply(trigger.address, "progress", text=text, in_reply_to=trigger.id,
                                             run_id=run_id, source="run"))

    # ---- interface reports --------------------------------------------------------------------------
    def observe(self, address: Address, text: str, direction: str, sender: Sender | None, kind: str,
                message_id: str | None, at: float | None, reply_to: str | None = None,
                quoted_text: str = "") -> None:
        if direction not in ("in", "out"):
            raise ValueError("direction must be in or out")
        if message_id and self.tank.has_message(str(address), direction, message_id):
            return
        self.tank.record_message(str(address), direction, text, kind=kind, sender_id=sender.id if sender else "",
                                 sender_name=sender.name if sender else "", message_id=message_id,
                                 reply_to=reply_to, quoted_text=quoted_text, at=at)

    def check_reply(self, reply: Reply) -> str | None:
        if reply.source != "run" or not reply.run_id:
            return None
        record = self.tank.run(reply.run_id)
        return "run_closed" if record is None else self._closed(record)

    async def cancel_interface(self, interface: str, reason: str) -> int:
        records = [record for record in self.tank.runs(("running", "waiting_input", "waiting_approval"), limit=1000)
                   if record.address.startswith(interface + ":")]
        for record in records:
            await self._cancel_record(record, reason)
        return len(records)

    async def relay(self, request: Request, origin: Address, refused: str | None = None) -> None:
        """An owner's message is sent on to another conversation (a reply to an event, or a forward)."""
        if refused is not None:
            await self._deliver(Reply(request.address, "notice", code="relayed", in_reply_to=request.id,
                                      data={"address": str(origin), "ok": "false", "detail": refused}))
            return
        delivery = await self._deliver(Reply(origin, "text", text=request.text, source="owner",
                                             data={"relayed_from": str(request.address)}))
        if delivery.ok:
            self.tank.record_message(str(origin), "out", request.text, message_id=delivery.message_id,
                                     sender_id=request.sender.id, sender_name=request.sender.name)
        await self._deliver(Reply(request.address, "notice", code="relayed", in_reply_to=request.id,
                                  data={"address": str(origin), "ok": "true" if delivery.ok else "false",
                                        "detail": delivery.detail}))

    async def cancel_conversation(self, address: Address, reason: str) -> int:
        records = self.tank.runs(("running", "waiting_input", "waiting_approval"), address=str(address), limit=100)
        for record in records:
            await self._cancel_record(record, reason)
        return len(records)

    async def _deliver_result(self, record: RunRecord, result: ToolResult) -> None:
        if result.summary.strip():
            await self.reply(record.id, result.summary)
        if result.status == "success":
            for file in result.files:
                await self.send_file(record.id, file)

    # ---- commands ---------------------------------------------------------------------------------
    async def command(self, request: Request) -> None:
        text = request.text.strip()[1:]
        name, _, rest = text.partition(" ")
        name, rest = name.strip().lower(), rest.strip()
        owner = self.is_owner(request)
        self.tank.record_message(str(request.address), "in", request.text, sender_id=request.sender.id,
                                 sender_name=request.sender.name, message_id=request.id, at=request.received_at)

        async def say(message: str) -> None:
            await self._deliver(Reply(request.address, "text", text=message, in_reply_to=request.id))

        if name in ("approve", "deny"):
            await say(await self.decide(rest, name == "approve", request) if rest else f"Usage: ${name} <id>")
        elif name == "runs":
            runs = self.tank.runs(limit=20, address=None if owner else str(request.address))
            pending = {item["run_id"]: item["id"] for item in self.tank.approvals("pending")}
            lines = [f"{run.id} {run.status} {run.address} {run.engine}"
                     + (f" approval={pending[run.id]}" if run.id in pending else "") for run in runs]
            await say("\n".join(lines) or "No open runs.")
        elif name == "cancel":
            await say(await self.cancel(rest or None, request))
        elif name == "memory":
            await say(self.memory_command(rest, request))
        elif name == "tools":
            assignment = self.assignment_for(request.address)
            names = [c.name for c in self.capabilities.values()
                     if assignment and assignment.allows(c.name) and (owner or not c.owner_only)]
            await say(", ".join(sorted(names)) or "No tools here.")
        elif name == "call":
            target, _, raw = rest.partition(" ")
            capability = self.capabilities.get(target)
            if not owner:
                await say("Only the owner can use $call.")
            elif capability is None:
                await say(f"No capability named {target!r}. Usage: $call <capability> <json arguments>")
            else:
                try:
                    arguments = json.loads(raw) if raw.strip() else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as error:
                    await say(f"Invalid arguments: {error}")
                else:
                    await self._command_run(request, capability, "", owner, arguments)
        elif name == "help":
            commands = [f"${c.command}" for c in self.capabilities.values() if c.command]
            await say("Commands: " + ", ".join(["$approve <id>", "$deny <id>", "$runs", "$cancel [run|address]", "$tools",
                                                "$memory",
                                                "$call <capability> <json>",
                                                *sorted(commands)]))
        else:
            capability = next((c for c in self.capabilities.values() if c.command == name), None)
            if capability is None:
                await self._deliver(Reply(request.address, "notice", code="unknown_command", in_reply_to=request.id,
                                          data={"command": name}))
                return
            await self._command_run(request, capability, rest, owner)

    def memory_command(self, text: str, request: Request) -> str:
        """`$memory`: what personas keep about the sender in this conversation; only theirs, only here."""
        address, subject = str(request.address), request.sender.id
        code = (lambda value: f"`{value}`") if request.display.markdown else str
        usage = ("Personas keep short notes about you in this conversation.\n"
                 f"{code('$memory [persona] [page]')} read them\n"
                 f"{code('$memory forget <note-id>')} remove one note\n"
                 f"{code('$memory clear [persona]')} remove all notes about you here, or one persona's")
        counts = self.tank.note_counts(address, subject)

        def persona_named(name: str) -> str | None:
            name = name.lower()
            return next((key for key, persona in self.personas.items()
                         if name in (key.lower(), persona.display_name.lower())), None)

        def label(persona: str) -> str:
            return self.personas[persona].display_name if persona in self.personas else persona

        parts = text.split()
        action = parts[0].lower() if parts else ""
        if action == "forget":
            if len(parts) != 2 or not parts[1].lstrip("#").isdecimal() or len(parts[1]) > 18:
                return usage
            removed = self.tank.delete_notes(address, subject, note_id=int(parts[1].lstrip("#")))
            self.audit.record("diary_forget", address=address, removed=removed)
            return "Note removed." if removed else "No note with that ID about you here."
        if action == "clear":
            persona = persona_named(parts[1]) if len(parts) == 2 else None
            if len(parts) > 2 or (len(parts) == 2 and persona is None):
                return usage
            removed = self.tank.delete_notes(address, subject, persona)
            self.audit.record("diary_clear", address=address, persona=persona or "*", removed=removed)
            return f"Removed {removed} note(s) about you here" + (f" from {label(persona)}." if persona else ".")
        page_text = parts[-1] if parts and parts[-1].isdecimal() else "1"
        names = parts[:-1] if parts and parts[-1].isdecimal() else parts
        if not names and len(counts) != 1:
            if not counts:
                return "Nobody keeps notes about you in this conversation.\n" + usage
            return "\n".join([*(f"{label(name)}: {count} note(s)" for name, count in counts.items()), usage])
        persona = persona_named(" ".join(names)) if names else next(iter(counts))
        if persona is None or len(page_text) > 6:
            return usage
        count = counts.get(persona, 0)
        if not count:
            return f"{label(persona)} keeps no notes about you in this conversation."
        pages = (count + 19) // 20
        page = int(page_text)
        if not 1 <= page <= pages:
            return f"Page {page} not found; pages 1-{pages}."
        lines = [f"{label(persona)}'s notes about you (page {page}/{pages})"]
        for note in self.tank.notes(persona, address, subject, 20, (page - 1) * 20):
            day = dt.datetime.fromtimestamp(note.updated_at).astimezone().strftime("%Y-%m-%d")
            lines.append(f"{code('#' + str(note.id))} {day} · {note.note}")
        if page < pages:
            lines.append(f"Next: {code(f'$memory {persona} {page + 1}')}")
        return "\n".join(lines)

    async def _command_run(self, request: Request, capability: Capability, text: str, owner: bool,
                           arguments: dict | None = None) -> None:
        assignment = self.assignment_for(request.address)
        if assignment is None:
            await self._deliver(Reply(request.address, "notice", code="not_permitted", in_reply_to=request.id))
            return
        if arguments is None:
            arguments = {capability.command_argument: text} if capability.command_argument and text else {}
        record = RunRecord(id=uuid.uuid4().hex[:12], address=str(request.address),
                           lane=f"{request.address}$command", engine=COMMAND_ENGINE,
                           personas=list(assignment.personas[:1]), assignment=assignment.to_dict(),
                           status="running", origin=request.origin, trigger=request_to_dict(request),
                           usage={"model_calls": 0, "tool_calls": 0, "seconds": 0.0})
        self.tank.create_run(record)
        # An explicit $command from the owner is the owner's approval of that exact action.
        result = await self.call_tool(record.id, capability.name, arguments, approved=owner)
        if result.status == "needs_approval":
            self.tank.update_run(record.id, status="waiting_approval")
            await self._deliver(Reply(request.address, "notice", code="waiting_approval", in_reply_to=request.id,
                                      run_id=record.id, data={"approval_id": result.data.get("approval_id", "")}))
            return
        self.tank.update_run(record.id, status="done")
        await self._deliver_result(record, result)

    async def cancel(self, target: str | None, request: Request) -> str:
        if target and ":" in target:
            if not self.is_owner(request):
                return "Only the owner can cancel a whole conversation."
            count = await self.cancel_conversation(Address.parse(target), f"cancelled by {request.address}")
            return f"Cancelled {count} run(s) in {target}."
        open_statuses = ("running", "waiting_input", "waiting_approval")
        if target:
            record = self.tank.run(target)
        else:
            mine = [run for run in self.tank.runs(open_statuses, address=str(request.address))
                    if request_from_dict(run.trigger).sender.id == request.sender.id]
            record = mine[0] if mine else None
        if record is None or record.status not in open_statuses:
            return "No open run to cancel."
        trigger = request_from_dict(record.trigger)
        if not (self.is_owner(request) or (trigger.address == request.address and trigger.sender == request.sender)):
            return "Only the owner or the person who started it can cancel this run."
        await self._cancel_record(record, f"cancelled by {request.address}")
        return f"Cancelled run {record.id}."

    async def _cancel_record(self, record: RunRecord, reason: str) -> None:
        async with self._locks[record.address]:
            current = self.tank.run(record.id)
            if current.status not in ("running", "waiting_input", "waiting_approval"):
                return
            self.tank.update_run(record.id, status="cancelled", error=reason[:500])
            for approval in self.tank.approvals("pending", record.id):
                self.tank.decide_approval(approval["id"], "expired", reason[:100])
        task = self._tasks.get(record.id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.audit.record("run_cancelled", run=record.id)

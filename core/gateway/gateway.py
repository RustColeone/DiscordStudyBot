"""The Gateway: the single entry point and the micromanager of the edge.

It admits requests, keeps per-conversation order, drops duplicates, routes replies, fires routines
and keeps its own bottle (gateway.db). It never formats output, calls models or touches the tank.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import datetime as dt
import secrets
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Mapping

from contracts import (Address, Delivery, Display, Interface, Operation, Reply, Request, Sender, SubmitResult,
                       ToolResult)
from core.audit import Audit
from core.gateway import routines as schedules
from core.gateway.routines import next_fire
from core.manager.policy import most_specific
from core.storage.bottle import Bottle
from core.storage.sqlite import ScopedStore

if TYPE_CHECKING:
    from core.manager.manager import Manager

ROUTINE = "routine"
RULE_KEYS = {"enabled", "allow_senders", "require_mention_in_groups", "owner_only", "max_age_s", "commands"}
REPLY_KINDS = {"text", "file", "progress", "notice", "event"}
DEFAULT_RULE = {"enabled": True, "require_mention_in_groups": True}


class Port:
    """Implements contracts.InterfacePort for one interface."""

    def __init__(self, gateway: "Gateway", name: str):
        self._gateway, self.name = gateway, name
        self.bottle = gateway.bottle.interface_store(name)

    async def submit(self, request: Request) -> SubmitResult:
        return await self._gateway.submit(self.name, request)

    async def notify(self, text: str, *, origin: Address | None = None, code: str = "event",
                     data: Mapping[str, str] | None = None, files=()) -> Delivery:
        if origin is not None and origin.interface != self.name:
            raise ValueError(f"Interface {self.name!r} cannot notify for {origin.interface!r}")
        return await self._gateway.notify(self.name, text, origin, code, data or {}, tuple(files))

    async def observe(self, address: Address, text: str, *, direction: str = "in", sender: Sender | None = None,
                      kind: str = "text", message_id: str | None = None, at: float | None = None,
                      reply_to: str | None = None, quoted_text: str = "", files=()) -> None:
        self._own(address)
        self._gateway.manager.observe(address, text, direction, sender, kind, message_id, at, reply_to, quoted_text,
                                      tuple(files))

    async def cancel(self, address: Address, reason: str) -> int:
        self._own(address)
        return await self._gateway.manager.cancel_conversation(address, f"{self.name}: {reason}")

    async def cancel_all(self, reason: str) -> int:
        return await self._gateway.manager.cancel_interface(self.name, f"{self.name}: {reason}")

    def check(self, reply: Reply) -> str | None:
        self._own(reply.address)
        return self._gateway.check(reply)

    def policy(self, address: Address) -> dict:
        self._own(address)
        assignment = self._gateway.manager.assignment_for(address)
        return {"admission": self._gateway.rule_for(address),
                "assignment": assignment.to_dict() if assignment is not None else None}

    def set_ready(self, ready: bool, detail: str = "") -> None:
        self._gateway.set_ready(self.name, ready, detail)

    def set_owners(self, sender_ids, control: Address | None = None, *, fallback: bool = False) -> None:
        if control is not None:
            self._own(control)
        self._gateway.manager.report_owners(self.name, list(sender_ids), control, fallback=fallback)

    def _own(self, address: Address) -> None:
        if address.interface != self.name:
            raise ValueError(f"Interface {self.name!r} cannot act for {address.interface!r}")

    def commands(self):
        return self._gateway.manager.commands()

    def models_ready(self, profile: str) -> bool:
        return self._gateway.manager.providers.profile_ready(str(profile))

    def publish(self, *, link: str | None = None, status=None) -> None:
        self._gateway.publish(self.name, link, status)

    def directory(self):
        return self._gateway.directory()

    def approval_status(self, approval_id: str) -> str | None:
        return self._gateway.approval_status(self.name, str(approval_id))

    def personas(self):
        return self._gateway.manager.persona_views()

    def prices(self):
        return self._gateway.manager.providers.prices

    def spending(self, days: int = 14):
        return self._gateway.manager.stamina.history(days)

    def stamina(self, persona: str | None = None):
        manager = self._gateway.manager
        if not persona:
            return manager.stamina.view_total()
        return manager.stamina.view(str(persona)) if str(persona) in manager.personas else None

    def facts(self, persona: str, sender_id: str | None = None):
        person = (self.name, str(sender_id)) if sender_id not in (None, "") else None
        return self._gateway.manager.facts.read(str(persona), person)

    def log(self, event: str, **fields: Any) -> None:
        self._gateway.audit.record("interface", interface=self.name, detail=event, **fields)


class Gateway:
    def __init__(self, bottle: Bottle, manager: "Manager", *, audit: Audit | None = None,
                 rules: Mapping[str, dict] | None = None, routes: Mapping[str, dict] | None = None,
                 routines: Mapping[str, dict] | None = None, tick_seconds: float = 1.0, clock=time.time):
        self.bottle, self.manager = bottle, manager
        self.audit = audit or Audit()
        self.interfaces: dict[str, Interface] = {}
        self._origin: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()
        self._timer: asyncio.Task | None = None
        self._tick_seconds, self._clock = tick_seconds, clock
        self._admin = GatewayAdmin(self)
        self._ready: dict[str, tuple[bool, str]] = {}
        self._published: dict[str, dict] = {}  # interface → {"link", "status"}: kept in memory, republished at start
        self._started = False
        self._lock_told: dict[str, float] = {}
        self.forwards = ScopedStore(bottle.db, "kv", "core:forwards")
        for pattern, rule in (rules or {}).items():
            self._admin.set_rule(pattern, rule)
        for pattern, route in (routes or {}).items():
            self._admin.set_route(pattern, route)
        for name, routine in (routines or {}).items():
            existing = self.bottle.routines().get(name)
            # Config defines the routine; keep the stored schedule position across restarts.
            self._admin.set_routine(name, routine, keep_next=existing)

    # ---- registration and lifecycle ----------------------------------------------------------------
    def register_interface(self, interface: Interface, extension: str) -> None:
        name = interface.name
        if name == ROUTINE or not name or ":" in name:
            raise ValueError(f"Interface name {name!r} is reserved or invalid")
        if name in self.interfaces:
            raise ValueError(f"Interface {name!r} is already registered")
        self.interfaces[name] = interface
        self._origin[name] = extension

    async def start(self) -> None:
        for name, interface in self.interfaces.items():
            try:
                await interface.start(Port(self, name))
                self.audit.record("interface_started", interface=name)
            except Exception as error:
                self.audit.record("interface_start_failed", interface=name, error=f"{type(error).__name__}: {error}")
        self._timer = asyncio.create_task(self._timer_loop(), name="gateway-timers")
        self._started = True

    async def stop(self) -> None:
        self._started = False
        if self._timer is not None:
            self._timer.cancel()
            await asyncio.gather(self._timer, return_exceptions=True)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for name, interface in self.interfaces.items():
            try:
                await interface.stop()
            except Exception as error:
                self.audit.record("interface_stop_failed", interface=name, error=type(error).__name__)

    # ---- inbound ----------------------------------------------------------------------------------
    @staticmethod
    def is_command(request: Request) -> bool:
        text = request.text.strip()
        return len(text) > 1 and text[0] == "$" and text[1].isalpha()

    def rule_for(self, address: Address) -> dict:
        return {**DEFAULT_RULE, **(most_specific(self.bottle.rules(), address)[1] or {})}

    def admit(self, request: Request) -> str:
        if request.origin != "person":
            return "accepted"
        rule = self.rule_for(request.address)
        owner = self.manager.is_owner(request)
        if not rule.get("enabled", True):
            return "disabled"
        if request.received_at < float(rule.get("since", 0)):
            return "before_rule_change"
        if rule.get("max_age_s") is not None and self._clock() - request.received_at > float(rule["max_age_s"]):
            return "too_old"
        if rule.get("owner_only") and not owner:
            return "owner_only"
        allowed = rule.get("allow_senders")
        if allowed is not None and request.sender.id not in set(map(str, allowed)) and not owner:
            return "sender_not_allowed"
        if not request.is_direct and rule.get("require_mention_in_groups", True) and request.mentions_me is not True:
            return "not_mentioned"
        if self.manager.locked and self.manager.role_of(request) == "member":
            return "locked"
        return "accepted"

    async def submit(self, interface: str, request: Request) -> SubmitResult:
        if request.address.interface != interface:
            raise ValueError(f"Interface {interface!r} cannot submit for {request.address.interface!r}")
        if request.origin != "person":
            raise ValueError("Interfaces submit person requests only")
        return await self._submit(request)

    async def _submit(self, request: Request) -> SubmitResult:
        key = f"{request.address.interface}:{request.id}"
        async with self._locks[str(request.address)]:
            if not self.bottle.mark_seen(key):
                self.audit.record("duplicate_dropped", address=str(request.address))
                return SubmitResult(False, "duplicate")
            try:
                owner = self.manager.is_owner(request)
                command = self.is_command(request) and (
                    owner or self.rule_for(request.address).get("commands", "all") == "all")
                if owner and not command and request.origin == "person":
                    receipt = (self.bottle.receipt(f"{request.address.interface}:{request.reply_to}")
                               if request.reply_to else None)
                    origin = receipt or self.forwards.get(str(request.address))
                    if origin is not None:
                        target = Address.parse(origin)
                        # Replying to one notification is the owner's own message to that chat, so it
                        # goes even where the bot does not answer; standing forwards need admission.
                        refused = (None if receipt or self.rule_for(target).get("enabled", True)
                                   else "not_admitted")
                        self.audit.record("admission", address=str(request.address), decision="relayed")
                        self._spawn(self.manager.relay(request, target, refused))
                        return SubmitResult(True, "relayed")
                decision = "accepted" if command and owner else self.admit(request)
                self.audit.record("admission", address=str(request.address), decision=decision, command=command)
                if decision == "locked":
                    self._tell_locked(request)
                if decision != "accepted":
                    return SubmitResult(False, decision)
                if command:
                    self._spawn(self.manager.command(request))
                    return SubmitResult(True, "command")
                await self.manager.accept(request)
                return SubmitResult(True, "accepted")
            except BaseException:
                # Not durably accepted: allow the interface to submit it again.
                self.bottle.forget_seen(key)
                raise

    def _tell_locked(self, request: Request) -> None:
        """While locked, each conversation hears once (per lock) that the bot is in maintenance."""
        epoch = float(self.manager.settings.get("lock_epoch") or 0)
        key = str(request.address)
        if self._lock_told.get(key) == epoch:
            return
        self._lock_told[key] = epoch
        self._spawn(self.deliver(Reply(request.address, "notice", code="locked", in_reply_to=request.id,
                                       text="The bot is in maintenance right now; please try again later.")))

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- outbound ---------------------------------------------------------------------------------
    def _target(self, reply: Reply) -> Address | None:
        route = most_specific(self.bottle.routes(), reply.address)[1]
        if reply.kind == "event":
            # Events are for the owner: only routes that name the event kind redirect them.
            return Address.parse(route["target"]) if route and "event" in (route.get("kinds") or []) \
                else self.manager.control
        if route and (not route.get("kinds") or reply.kind in route["kinds"]):
            return Address.parse(route["target"])
        if reply.address.interface == ROUTINE:
            return self.manager.control
        interface = self.interfaces.get(reply.address.interface)
        if reply.kind == "progress" and interface is not None and not interface.display.progress:
            return self.manager.control
        return reply.address

    def target(self, reply: Reply) -> Address | None:
        """Where a reply would be delivered (after routes)."""
        return self._target(reply)

    async def deliver(self, reply: Reply) -> Delivery:
        target = self._target(reply)
        if target is None:
            return Delivery(False, detail="no_route")
        interface = self.interfaces.get(target.interface)
        if interface is None:
            return Delivery(False, detail="no_interface")
        if not self._ready.get(target.interface, (True, ""))[0]:
            return Delivery(False, detail="interface_not_ready")
        refusal = self._admission_refusal(reply, target)
        if refusal is not None:
            return Delivery(False, detail=refusal)
        if target != reply.address:
            data = dict(reply.data)
            data.setdefault("origin_address", str(reply.address))
            reply = dataclasses.replace(reply, address=target, quote=None, data=data)
        try:
            return await interface.deliver(reply)
        except Exception as error:
            self.audit.record("delivery_error", interface=target.interface, error=type(error).__name__)
            return Delivery(False, detail=f"{type(error).__name__}: {error}"[:300], uncertain=True)

    def _admission_refusal(self, reply: Reply, target: Address) -> str | None:
        """Run output goes only to conversations that are still admitted (both origin and routed target)."""
        if reply.source != "run":
            return None
        for address in {reply.address, target}:
            if address.interface != ROUTINE and not self.rule_for(address).get("enabled", True):
                return "not_admitted"
        return None

    def check(self, reply: Reply) -> str | None:
        return self.manager.check_reply(reply) or self._admission_refusal(reply, reply.address)

    async def operate(self, interface: str, operation: str, arguments: Mapping[str, Any],
                      context: Any = None) -> ToolResult:
        target = self.interfaces.get(interface)
        if target is None:
            return ToolResult.fail(f"Interface {interface!r} is not running", "no_interface")
        try:
            result = await target.operate(operation, dict(arguments), context)
        except Exception as error:
            return ToolResult.fail(f"{interface}.{operation} failed: {type(error).__name__}: {error}"[:1000],
                                   type(error).__name__)
        return result if isinstance(result, ToolResult) else ToolResult.fail("Invalid operation result",
                                                                            "invalid_result")

    async def notify(self, interface: str, text: str, origin: Address | None, code: str,
                     data: Mapping[str, str], files: tuple) -> Delivery:
        source = origin or Address(interface, "_interface")
        payload = {**dict(data), "interface": interface}
        delivery = await self.deliver(Reply(source, "event", text=text, code=code, data=payload))
        target = self._target(Reply(source, "event"))
        if delivery.ok and origin is not None and delivery.message_id and target is not None:
            self.bottle.add_receipt(f"{target.interface}:{delivery.message_id}", str(origin))
        for file in files:
            await self.deliver(Reply(source, "event", text="", file=file, code=code, data=payload))
        self.audit.record("interface_event", interface=interface, code=code, ok=delivery.ok)
        return delivery

    def set_ready(self, interface: str, ready: bool, detail: str = "") -> None:
        previous = self._ready.get(interface)
        self._ready[interface] = (bool(ready), detail)
        if previous is None and ready:
            return
        if previous is None or previous[0] != bool(ready):
            self.audit.record("interface_ready", interface=interface, ready=bool(ready))
            self._spawn(self.notify(interface, detail, None, "interface_ready" if ready else "interface_not_ready",
                                    {"detail": detail}, ()))

    def publish(self, interface: str, link: str | None, status) -> None:
        """An interface's link and public status, for the others (InterfacePort.publish, contracts 1.7)."""
        entry = dict(self._published.get(interface) or {"link": "", "status": {}})
        if link is not None:
            if not isinstance(link, str) or len(link) > 2048 or (link and not link.startswith(("http://", "https://"))):
                raise ValueError("link must be an http(s) URL of at most 2048 characters")
            entry["link"] = link
        if status is not None:
            try:  # JSON only, as browsers read it (no NaN): it goes to other modules and pages as data
                text = json.dumps(status, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError(f"status must be plain JSON: {error}") from None
            if len(text.encode("utf-8")) > 16384 or not isinstance(status, dict):
                raise ValueError("status must be a JSON object of at most 16 KB")
            shape = {"place": dict, "people": list, "away": list, "tickets": list}  # contracts.PLACE_STATUS
            wrong = [name for name, kind in shape.items() if name in status and not isinstance(status[name], kind)]
            wrong += [name for name in ("people", "tickets") if isinstance(status.get(name), list)
                      and not all(isinstance(item, dict) for item in status[name])]
            if wrong:
                raise ValueError(f"a place's status has the wrong shape for {', '.join(wrong)} (contracts.PLACE_STATUS)")
            entry["status"] = json.loads(text)
        self._published[interface] = entry

    def approval_status(self, interface: str, approval_id: str) -> str | None:
        """pending | approved | denied | expired, for an approval asked in one of `interface`'s conversations (None for
        any other): a pending one whose run is no longer open counts as expired."""
        manager = self.manager
        record = manager.tank.approval(approval_id)
        if record is None or not str(record.get("address", "")).startswith(f"{interface}:"):
            return None
        if record["status"] != "pending":
            return record["status"]
        run = manager.tank.run(record["run_id"])
        return "pending" if run is not None and run.status in ("running", "waiting_input", "waiting_approval") \
            else "expired"

    def directory(self) -> dict[str, dict]:
        """Every interface with what it published (copies), and whether it is ready."""
        return {name: {"link": (self._published.get(name) or {}).get("link", ""),
                       "status": json.loads(json.dumps((self._published.get(name) or {}).get("status", {}))),
                       "ready": self._ready.get(name, (True, ""))[0]} for name in self.interfaces}

    def ready(self) -> dict[str, tuple[bool, str]]:
        return {name: self._ready.get(name, (True, "")) for name in self.interfaces}

    def interface_operations(self) -> dict[str, tuple[Operation, ...]]:
        return {name: tuple(interface.operations) for name, interface in self.interfaces.items()}

    def admin(self) -> "GatewayAdmin":
        return self._admin

    def sweep_disabled(self) -> None:
        """A rule that stops admitting a conversation also cancels that conversation's open runs."""
        for record in self.manager.tank.runs(("running", "waiting_input", "waiting_approval"), limit=1000):
            address = Address.parse(record.address)
            if address.interface != ROUTINE and not self.rule_for(address).get("enabled", True):
                self._spawn(self.manager.cancel_conversation(address, "not_admitted"))

    def now(self) -> float:
        return self._clock()

    def display_for(self, address: Address) -> Display:
        target = self._target(Reply(address, "text"))
        interface = self.interfaces.get(target.interface) if target else None
        return interface.display if interface is not None else Display()

    # ---- routines ---------------------------------------------------------------------------------
    async def _timer_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.audit.record("timer_error", error=type(error).__name__)
            await asyncio.sleep(self._tick_seconds)

    async def tick(self, now: float | None = None) -> list[str]:
        """Fire every due routine once. Returns the names fired."""
        now = self._clock() if now is None else now
        fired = []
        for name, routine in self.bottle.routines().items():
            if not routine.get("enabled", True) or routine.get("next_at", float("inf")) > now:
                continue
            if routine.get("address"):
                if await self._fire_conversation_routine(name, routine, now):
                    fired.append(name)
                continue
            address = Address(ROUTINE, name)
            request = Request(id=f"{name}:{int(routine['next_at'])}", address=address,
                              sender=Sender(f"routine:{name}", name, "system"), text=routine["text"],
                              received_at=now, origin="routine", display=self.display_for(address))
            await self._submit(request)
            routine = {**routine, "last_at": now, "next_at": next_fire(routine, now)}
            self.bottle.set_routine(name, routine)
            fired.append(name)
            self.audit.record("routine_fired", routine=name)
        await self.fire_scheduled(now)
        return fired

    async def _fire_conversation_routine(self, routine_id: str, routine: dict, now: float) -> bool:
        """Post the routine's text to its conversation as its creator; then schedule the next time.
        Skipped (not deleted) while the conversation is closed or a lock keeps its creator out."""
        address = Address.parse(routine["address"])
        sender = Sender(str(routine["sender"]["id"]), str(routine["sender"].get("name", "")), "person")
        request = Request(id=f"routine:{routine_id}:{int(routine['next_at'])}", address=address, sender=sender,
                          text=routine["text"], received_at=now, origin="routine",
                          is_direct=bool(routine.get("is_direct")), mentions_me=True,
                          display=self.display_for(address),
                          extra={"routine": routine_id, "routine_via": routine.get("via", "command")})
        reason = None
        if not self.rule_for(address).get("enabled", True):
            reason = "conversation_closed"
        elif self.manager.locked and self.manager.role_of(request) == "member":
            reason = "locked"
        if reason is None:
            await self._submit(request)
        upcoming = schedules.next_fire(routine, now)
        if upcoming is None:
            self.bottle.set_routine(routine_id, None)
        else:
            self.bottle.set_routine(routine_id, {**routine, "last_at": now, "next_at": upcoming})
        self.audit.record("routine_fired", routine=routine_id, address=str(address), skipped=reason)
        return reason is None

    # ---- conversation routines --------------------------------------------------------------------
    def add_routine(self, address: Address, sender: Sender, is_direct: bool, schedule: Mapping[str, Any],
                    text: str, via: str = "command") -> dict:
        """`via`: `command` (a person typed it) or `tool` (a model set it up; see Manager.vouched)."""
        schedule = schedules.validate(schedule)
        text = text.strip()
        if not text:
            raise ValueError("A routine needs something to send")
        if len(text) > schedules.MAX_TEXT:
            raise ValueError(f"A routine's text is at most {schedules.MAX_TEXT} characters")
        now = self._clock()
        upcoming = schedules.next_fire(schedule, now)
        if upcoming is None:
            raise ValueError("That time has already passed")
        routine_id = schedules.new_id()
        routine = {**schedule, "address": str(address), "sender": {"id": sender.id, "name": sender.name},
                   "is_direct": bool(is_direct), "text": text, "enabled": True, "created_at": now,
                   "next_at": upcoming, "via": "tool" if via == "tool" else "command"}
        self.bottle.set_routine(routine_id, routine)
        self.audit.record("routine_added", routine=routine_id, address=str(address))
        return {"id": routine_id, **routine}

    def conversation_routines(self, address: Address | None = None) -> list[dict]:
        """Conversation routines (of one conversation, or all), soonest first."""
        found = [{"id": name, **routine} for name, routine in self.bottle.routines().items()
                 if routine.get("address") and (address is None or routine["address"] == str(address))]
        return sorted(found, key=lambda item: item.get("next_at") or float("inf"))

    def configured_routines(self) -> list[dict]:
        return [{"id": name, **routine} for name, routine in self.bottle.routines().items()
                if not routine.get("address")]

    def update_routine(self, routine_id: str, *, enabled: bool | None = None, delete: bool = False) -> dict:
        routine = self.bottle.routines().get(routine_id)
        if routine is None:
            raise ValueError(f"No routine {routine_id!r}")
        if delete:
            self.bottle.set_routine(routine_id, None)
        else:
            routine = {**routine, "enabled": bool(enabled)}
            if enabled and routine.get("address"):
                upcoming = schedules.next_fire(routine, self._clock())
                if upcoming is None:
                    raise ValueError("That one-off time has already passed")
                routine["next_at"] = upcoming
            self.bottle.set_routine(routine_id, routine)
        self.audit.record("routine_" + ("deleted" if delete else "enabled" if enabled else "paused"),
                          routine=routine_id)
        return {"id": routine_id, **routine}

    MAX_SCHEDULED_PER_CONVERSATION = 50
    MAX_SCHEDULE_AHEAD_S = 366 * 86400

    def schedule(self, address: Address, at: float, text: str, sender: Sender) -> str:
        now = self._clock()
        if not text.strip():
            raise ValueError("A scheduled message needs text")
        if at <= now or at - now > self.MAX_SCHEDULE_AHEAD_S:
            raise ValueError("The time must be in the future and within a year")
        if len(self.bottle.scheduled(str(address))) >= self.MAX_SCHEDULED_PER_CONVERSATION:
            raise ValueError("This conversation already has the maximum number of scheduled messages")
        scheduled_id = secrets.token_hex(3)
        self.bottle.add_scheduled({"id": scheduled_id, "address": str(address), "at": float(at), "text": text[:2000],
                                   "sender_id": sender.id, "sender_name": sender.name, "created_at": now})
        self.audit.record("scheduled", address=str(address), scheduled=scheduled_id)
        return scheduled_id

    def scheduled_for(self, address: Address) -> list[dict]:
        return [{key: item[key] for key in ("id", "at", "text", "sender_id", "sender_name")}
                for item in self.bottle.scheduled(str(address))]

    def cancel_scheduled(self, address: Address, scheduled_id: str) -> bool:
        if scheduled_id not in {item["id"] for item in self.bottle.scheduled(str(address))}:
            return False
        return self.bottle.move_scheduled(scheduled_id, "cancelled")

    async def fire_scheduled(self, now: float) -> None:
        """Claim each due item, then deliver it in its own task so a slow interface never stalls the timer."""
        for item in self.bottle.due(now):
            if self.bottle.move_scheduled(item["id"], "firing"):
                self._spawn(self._fire_one(item))

    async def _fire_one(self, item: dict) -> None:
        address = Address.parse(item["address"])
        if not self.rule_for(address).get("enabled", True):
            self.bottle.move_scheduled(item["id"], "skipped", "firing")
            return
        delivery = await self.deliver(Reply(address, "notice", text=item["text"], code="reminder", source="core",
                                            data={"scheduled_id": item["id"], "sender_id": item["sender_id"],
                                                  "sender_name": item["sender_name"], "at": str(item["at"])}))
        if delivery.detail == "interface_not_ready":
            self.bottle.move_scheduled(item["id"], "pending", "firing")
            return
        status = "sent" if delivery.ok else "uncertain" if delivery.uncertain else "failed"
        self.bottle.move_scheduled(item["id"], status, "firing")
        if delivery.ok:
            self.manager.tank.record_message(str(address), "out", item["text"], kind="reminder",
                                             message_id=delivery.message_id)
        self.audit.record("scheduled_fired", scheduled=item["id"], status=status)


class GatewayAdmin:
    """Validated edits of gateway data, used by the core's own capabilities."""

    def __init__(self, gateway: Gateway):
        self._gateway = gateway

    def rules(self) -> dict:
        return self._gateway.bottle.rules()

    def set_rule(self, pattern: str, rule: Mapping[str, Any] | None) -> None:
        _check_pattern(pattern)
        if rule is not None:
            unknown = set(rule) - RULE_KEYS
            if unknown:
                raise ValueError(f"Unknown rule keys: {sorted(unknown)}; allowed: {sorted(RULE_KEYS)}")
            if rule.get("allow_senders") is not None and not isinstance(rule["allow_senders"], list):
                raise ValueError("allow_senders must be a list of sender IDs or null")
            if rule.get("commands", "all") not in ("all", "owners"):
                raise ValueError("commands must be all or owners")
            existing = self._gateway.bottle.rules().get(pattern)
            unchanged = existing is not None and {k: v for k, v in existing.items() if k != "since"} == dict(rule)
            # Messages received before a rule changes are judged by the old rule, which no longer exists:
            # they are not admitted, so enabling a conversation never answers its backlog.
            rule = {**dict(rule), "since": existing.get("since", 0) if unchanged else self._gateway._clock()}
        self._gateway.bottle.set_rule(pattern, rule)
        if self._gateway._started:
            self._gateway.sweep_disabled()

    def forwards(self) -> dict:
        store = self._gateway.forwards
        return {key: store.get(key) for key in store.keys()}

    def set_forward(self, address: str, target: str | None) -> None:
        source = Address.parse(address)
        if "*" in address or (target is not None and "*" in target):
            raise ValueError("Forwards connect two exact addresses")
        if target is None:
            self._gateway.forwards.delete(str(source))
        else:
            self._gateway.forwards.set(str(source), str(Address.parse(target)))

    def routes(self) -> dict:
        return self._gateway.bottle.routes()

    def set_route(self, pattern: str, route: Mapping[str, Any] | None) -> None:
        _check_pattern(pattern)
        if route is not None:
            if set(route) - {"target", "kinds"}:
                raise ValueError("A route has only target and kinds")
            Address.parse(route["target"])
            kinds = route.get("kinds")
            if kinds is not None and (not isinstance(kinds, list) or set(kinds) - REPLY_KINDS):
                raise ValueError(f"kinds must be a list drawn from {sorted(REPLY_KINDS)}")
            route = dict(route)
        self._gateway.bottle.set_route(pattern, route)

    def routines(self) -> dict:
        return self._gateway.bottle.routines()

    def set_routine(self, name: str, routine: Mapping[str, Any], keep_next: Mapping[str, Any] | None = None) -> dict:
        if not name or not name.replace("_", "").replace("-", "").isalnum():
            raise ValueError("Routine names use letters, digits, - and _")
        routine = {key: value for key, value in dict(routine).items() if value is not None}
        if set(routine) - {"text", "every_minutes", "daily_at", "enabled"}:
            raise ValueError("A routine has text, every_minutes or daily_at, and enabled")
        if not str(routine.get("text", "")).strip():
            raise ValueError("A routine needs instruction text")
        if bool(routine.get("every_minutes")) == bool(routine.get("daily_at")):
            raise ValueError("Give exactly one of every_minutes or daily_at")
        if routine.get("daily_at"):
            hour, _, minute = str(routine["daily_at"]).partition(":")
            if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
                raise ValueError("daily_at must be HH:MM")
        now = self._gateway._clock()
        next_at = keep_next.get("next_at") if keep_next else None
        routine["next_at"] = next_at if next_at is not None else next_fire(routine, now)
        routine.setdefault("enabled", True)
        self._gateway.bottle.set_routine(name, routine)
        return {**routine, "next_at_text": dt.datetime.fromtimestamp(routine["next_at"]).astimezone().isoformat(
            timespec="minutes")}

    def delete_routine(self, name: str) -> None:
        if name not in self._gateway.bottle.routines():
            raise ValueError(f"No routine named {name!r}")
        self._gateway.bottle.set_routine(name, None)


def _check_pattern(pattern: str) -> None:
    if pattern != "*":
        interface, separator, conversation = pattern.partition(":")
        if not separator or not interface or not conversation:
            raise ValueError("Patterns are '*', 'interface:*' or 'interface:conversation'")

"""Capabilities owned by the core itself (namespace `core`) plus interface operations.

They act on gateway data (routes, admission rules, routines) and on conversations, so most are
owner-only. Changing policy (rules, routes, forwards, routines, assignments) is critical and never runs
without the owner's approval when a model asks; sending elsewhere is critical, grantable per target.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any, Mapping

from contracts import Address, Capability, Command, Option, Reply, Risk, ToolResult
from core.gateway.routines import describe
from core.serialize import request_from_dict
from core.manager.handles import local_time as when
from core.manager.model_choice import model_arguments_tier
from core.manager.routines import Caller
from core.manager.stamina import money

if TYPE_CHECKING:
    from core.manager.manager import GatewayLink, Manager


def core_capabilities(manager: "Manager", gateway: "GatewayLink") -> list[Capability]:
    admin = gateway.admin()

    def guarded(function):
        async def run(arguments: Mapping[str, Any], context) -> ToolResult:
            try:
                return await function(dict(arguments), context)
            except (ValueError, KeyError) as error:
                return ToolResult.fail(str(error), "invalid_arguments")
        return run

    async def send_message(arguments, context) -> ToolResult:
        address = Address.parse(arguments["address"])
        delivery = await gateway.deliver(Reply(address, "text", text=arguments["text"], run_id=context.run_id,
                                                  source="owner"))
        if not delivery.ok:
            return ToolResult.fail(f"Delivery to {address} failed: {delivery.detail}",
                                   "delivery_uncertain" if delivery.uncertain else "delivery_failed")
        manager.tank.record_message(str(address), "out", arguments["text"], message_id=delivery.message_id,
                                    run_id=context.run_id)
        return ToolResult.ok(f"Sent to {address}.", data={"message_id": delivery.message_id or ""})

    def lister(getter, label):
        async def run(arguments, context) -> ToolResult:
            value = getter()
            return ToolResult.ok(json.dumps(value, ensure_ascii=False, indent=1) if value else f"No {label}.",
                                 data={label: value})
        return run

    async def set_rule(arguments, context) -> ToolResult:
        admin.set_rule(arguments["pattern"], arguments.get("rule"))
        return ToolResult.ok(f"Admission rule for {arguments['pattern']} updated.")

    async def set_route(arguments, context) -> ToolResult:
        admin.set_route(arguments["pattern"], arguments.get("route"))
        return ToolResult.ok(f"Route for {arguments['pattern']} updated.")

    async def set_routine(arguments, context) -> ToolResult:
        routine = admin.set_routine(arguments["name"], {key: value for key, value in arguments.items() if key != "name"})
        return ToolResult.ok(f"Routine {arguments['name']} saved; next run at {routine['next_at_text']}.")

    async def set_assignment(arguments, context) -> ToolResult:
        assignment = manager.set_assignment(arguments["pattern"], arguments["assignment"])
        return ToolResult.ok(f"Assignment for {arguments['pattern']} saved: engine {assignment.engine}, "
                             f"personas {list(assignment.personas)}, enabled {assignment.enabled}. "
                             "Messages received before now do not start runs under it.")

    async def delete_assignment(arguments, context) -> ToolResult:
        manager.delete_assignment(arguments["pattern"])
        return ToolResult.ok(f"Runtime assignment for {arguments['pattern']} deleted.")

    async def list_assignments(arguments, context) -> ToolResult:
        value = {pattern: assignment.to_dict() for pattern, assignment in manager.assignments.items()}
        return ToolResult.ok(json.dumps(value, ensure_ascii=False, indent=1), data={"assignments": value})

    async def set_forward(arguments, context) -> ToolResult:
        admin.set_forward(arguments["address"], arguments.get("target"))
        return ToolResult.ok(f"Forward from {arguments['address']} " + (f"to {arguments['target']} saved."
                             if arguments.get("target") else "removed."))

    async def cancel_runs(arguments, context) -> ToolResult:
        count = await manager.cancel_conversation(Address.parse(arguments["address"]), f"cancelled by run {context.run_id}")
        return ToolResult.ok(f"Cancelled {count} open run(s) in {arguments['address']}.")

    def caller(context) -> Caller:
        return Caller(context.address, context.sender, context.is_direct, context.access, context.display.markdown)

    async def add_routine(arguments, context) -> ToolResult:
        try:
            routine = manager.routines.add(caller(context), arguments["schedule"], arguments["text"],
                                           arguments.get("chat"),
                                           via="command" if manager.typed_call(context.run_id) else "tool")
        except PermissionError as error:
            return ToolResult.fail(str(error), "not_permitted")
        return ToolResult.ok(f"Routine {routine['id']} added: {describe(routine)}; it sends {routine['text']!r}.",
                             data={"id": routine["id"], "next_at": routine["next_at"]})

    async def list_routines(arguments, context) -> ToolResult:
        who = caller(context)
        items = manager.routines.visible(who)
        return ToolResult.ok(manager.routines.listing(who), data={"routines": items})

    def changer(action):
        async def run(arguments, context) -> ToolResult:
            try:
                routine = manager.routines.change(caller(context), arguments["id"], action)
            except LookupError as error:
                return ToolResult.fail(str(error), "not_found")
            except PermissionError as error:
                return ToolResult.fail(str(error), "not_permitted")
            return ToolResult.ok(f"Routine {routine['id']} {action}d." if action != "pause"
                                 else f"Routine {routine['id']} paused.")
        return run

    async def search_history(arguments, context) -> ToolResult:
        query = str(arguments["query"]).strip()
        if not query:
            return ToolResult.fail("What should I look for?", "invalid_arguments")
        record = manager.tank.run(context.run_id)
        trigger = request_from_dict(record.trigger).id if record is not None and record.trigger else None
        # Earlier than the request that asked: not the question itself, nor what came after it.
        found = manager.tank.search_messages(str(context.address), query, int(arguments.get("limit") or 10),
                                             before=trigger)
        if not found:
            return ToolResult.ok(f"Nothing earlier in this conversation mentions {query!r}.", data={"matches": 0})
        speaker = (lambda item: item.sender_name or ("assistant" if item.direction == "out" else item.sender_id))
        lines = [f"[{when(item.at)}] {speaker(item)}: {item.text[:400]}" for item in found]
        return ToolResult.ok("\n".join(lines), data={"matches": len(found)})

    async def model(arguments, context) -> ToolResult:
        return await manager.model_choices.command(context.address, str(arguments.get("text") or ""))

    async def stamina(arguments, context) -> ToolResult:
        record = manager.tank.run(context.run_id) if context.run_id else None
        return ToolResult.ok(manager.stamina.extend(arguments, float(record.usage.get("cost", 0)) if record else None))

    def stamina_card(arguments) -> str:
        """The question on the card a run waits with at a spending limit (core/manager/stamina.py)."""
        amount = money(float(arguments.get("amount") or 0))
        if arguments.get("kind") == "total":
            return (f"Everyone together has spent today's {money(float(arguments.get('limit') or 0))}. "
                    f"Let them spend {amount} more today?")
        if arguments.get("kind") == "session":
            return (f"The session spent its budget ({money(float(arguments.get('spent') or 0))} of "
                    f"{money(float(arguments.get('limit') or 0))}). Let it spend {amount} more?")
        name = manager.stamina.name(str(arguments.get("persona") or ""))
        return (f"{name} has spent her {money(float(arguments.get('limit') or 0))} for today. "
                f"Let her spend {amount} more today?")

    async def stamina_settings(arguments, context) -> ToolResult:
        return ToolResult.ok(manager.stamina.command(str(arguments.get("text") or "")))

    rule_schema = {"type": ["object", "null"], "description": "enabled, allow_senders, require_mention_in_groups; null deletes"}
    capabilities = [
        # Not a tool: the card a run waits with at a spending limit, carried out when the owner approves it.
        Capability("core.stamina", "Let a persona spend more today, or a session past its budget.", guarded(stamina),
                   {"kind": {"type": "string"}, "persona": {"type": "string"}, "amount": {"type": "number"},
                    "day": {"type": "string"}, "run": {"type": "string"}, "spent": {"type": "number"},
                    "limit": {"type": "number"}}, ("kind", "amount"), Risk.WRITE, internal=True,
                   describe=stamina_card),
        Capability("core.stamina_settings", "Show today's spending, or change everyone's daily total, a persona's daily "
                   "allowance or an account's budget.", guarded(stamina_settings),
                   {"text": {"type": "string", "description": "empty, 'total <dollars>', '<persona> <dollars>|reset' or "
                                                              "'budget <provider> <dollars>'"}},
                   risk=Risk.WRITE, internal=True, owner_only=True,
                   command=Command("stamina", "Show today's spending, or set the daily total, an allowance or a budget",
                                   (Option("text", "total <dollars>, <persona> <dollars>, or budget <provider> <dollars>"),),
                                   examples=("total 3", "deepseek 0.8", "budget openai 20")),
                   command_argument="text"),
        Capability("core.send_message", "Send a text message to another conversation (interface:conversation).",
                   guarded(send_message), {"address": {"type": "string"}, "text": {"type": "string"}},
                   ("address", "text"), Risk.CRITICAL, owner_only=True, subject="address", grantable=True,
                   describe=lambda a: f"Send to {a.get('address')}: {str(a.get('text', ''))[:300]}"),
        Capability("core.list_rules", "List gateway admission rules by address pattern.",
                   guarded(lister(admin.rules, "rules")), risk=Risk.READ, owner_only=True, private=True),
        Capability("core.set_rule", "Set or delete (rule=null) the admission rule for an address pattern.",
                   guarded(set_rule), {"pattern": {"type": "string"}, "rule": rule_schema}, ("pattern",),
                   Risk.CRITICAL, owner_only=True, never_auto=True),
        Capability("core.list_routes", "List reply routes by address pattern.",
                   guarded(lister(admin.routes, "routes")), risk=Risk.READ, owner_only=True, private=True),
        Capability("core.set_route", "Set or delete (route=null) where replies for an address pattern go. "
                   "route: {target: 'interface:conversation', kinds: ['text','file','progress','notice']}",
                   guarded(set_route), {"pattern": {"type": "string"}, "route": {"type": ["object", "null"]}},
                   ("pattern",), Risk.CRITICAL, owner_only=True, never_auto=True),
        Capability("core.add_routine",
                   "Repeat a message or a $command on a schedule in this conversation, sent as the person who "
                   "asked (plain text goes to the bot as their message; '$status' runs that command). schedule: "
                   "'every 30m', 'every 2h', 'daily 08:00', 'weekdays 09:00', 'mon,wed 18:30', 'at 18:30' or "
                   "'at 2026-10-17 08:00' (local time). chat: another conversation (admins only).",
                   guarded(add_routine), {"schedule": {"type": "string"}, "text": {"type": "string"},
                                          "chat": {"type": "string"}}, ("schedule", "text"), Risk.WRITE),
        Capability("core.list_routines", "List routines: this conversation's, or every conversation's for admins.",
                   guarded(list_routines), risk=Risk.READ),
        Capability("core.pause_routine", "Pause a routine by ID (your own here; admins: any).", guarded(changer("pause")),
                   {"id": {"type": "string"}}, ("id",), Risk.WRITE),
        Capability("core.resume_routine", "Resume a paused routine by ID.", guarded(changer("resume")),
                   {"id": {"type": "string"}}, ("id",), Risk.WRITE),
        Capability("core.set_routine", "Create or replace a routine that starts a run in routine:<name>. "
                   "Give every_minutes or daily_at (HH:MM, local time) and the instruction text.",
                   guarded(set_routine),
                   {"name": {"type": "string"}, "text": {"type": "string"},
                    "every_minutes": {"type": "number", "minimum": 1}, "daily_at": {"type": "string"},
                    "enabled": {"type": "boolean"}}, ("name", "text"), Risk.CRITICAL, owner_only=True,
                   never_auto=True),
        Capability("core.delete_routine", "Delete a routine by ID (your own here; admins: any).",
                   guarded(changer("delete")), {"id": {"type": "string"}}, ("id",), Risk.WRITE),
        Capability("core.list_assignments", "List engine/persona/policy assignments by address pattern.",
                   guarded(list_assignments), risk=Risk.READ, owner_only=True, private=True),
        Capability("core.set_assignment", "Create or replace the runtime assignment for an address pattern. "
                   "assignment: {engine, personas, budget, trusted, lane, enabled, options}. What may run there is set "
                   "under permissions in core.yml.",
                   guarded(set_assignment), {"pattern": {"type": "string"}, "assignment": {"type": "object"}},
                   ("pattern", "assignment"), Risk.CRITICAL, owner_only=True, never_auto=True),
        Capability("core.delete_assignment", "Delete a runtime assignment (configured ones stay).",
                   guarded(delete_assignment), {"pattern": {"type": "string"}}, ("pattern",), Risk.CRITICAL,
                   owner_only=True, never_auto=True),
        Capability("core.list_forwards", "List conversations whose owner messages are forwarded elsewhere.",
                   guarded(lister(admin.forwards, "forwards")), risk=Risk.READ, owner_only=True, private=True),
        Capability("core.set_forward", "Forward the owner's ordinary messages in one conversation (exact address) "
                   "to another (target), or remove it (target=null). The target must be admitted.",
                   guarded(set_forward), {"address": {"type": "string"}, "target": {"type": ["string", "null"]}},
                   ("address",), Risk.CRITICAL, owner_only=True, never_auto=True),
        # Only this conversation's own messages, which its members could scroll back to anyway.
        Capability("core.search_history", "Find earlier messages of this conversation that contain a word or "
                   "phrase, beyond what you can see of it (newest first, with time and speaker). Use it when the "
                   "conversation's summary or recent messages are not enough.", guarded(search_history),
                   {"query": {"type": "string", "description": "A word or short phrase, as it would appear"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ("query",), Risk.READ),
        # Typed by people only (internal: never offered to models, so no model picks its own model).
        Capability("core.model", "Show which model each persona answers with here and which models the providers "
                   "offer, or choose one for a persona in this conversation.",
                   guarded(model), {"text": {"type": "string", "description": "empty, a provider, "
                                             "'<persona> <provider>/<model>' or '<persona> reset'"}},
                   risk=Risk.READ, escalate=model_arguments_tier, internal=True, access="admin", timeout_s=60,
                   command=Command("model", "Show or choose the model each persona answers with here",
                                   (Option("text", "a provider, or <persona> <provider>/<model> (or reset)"),),
                                   examples=("grok grok/grok-4.3", "grok reset", "openai"), access="admin"),
                   command_argument="text"),
        Capability("core.cancel_runs", "Cancel all open runs in a conversation (interface:conversation).",
                   guarded(cancel_runs), {"address": {"type": "string"}}, ("address",), Risk.WRITE, owner_only=True),
    ]
    for interface, operations in gateway.interface_operations().items():
        for operation in operations:
            capabilities.append(_operation_capability(gateway, interface, operation))
    return capabilities


def _operation_capability(gateway: "GatewayLink", interface: str, operation) -> Capability:
    async def run(arguments, context) -> ToolResult:
        return await gateway.operate(interface, operation.name, arguments, context)
    return Capability(f"{interface}.{operation.name}", operation.description, run, dict(operation.parameters),
                      tuple(operation.required), operation.risk, owner_only=operation.owner_only,
                      command=operation.command,
                      command_argument=operation.command_argument, timeout_s=operation.timeout_s,
                      private=operation.private, subject=operation.subject, subject_kind=operation.subject_kind,
                      never_auto=operation.never_auto, grantable=operation.grantable, confirm=operation.confirm)

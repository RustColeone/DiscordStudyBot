"""Capabilities owned by the core itself (namespace `core`) plus interface operations.

They act on gateway data (routes, admission rules, routines) and on conversations, so they are
owner-only; changes are writes and go through approval unless pre-approved.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Mapping

from contracts import Address, Capability, Reply, Risk, ToolResult

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

    async def delete_routine(arguments, context) -> ToolResult:
        admin.delete_routine(arguments["name"])
        return ToolResult.ok(f"Routine {arguments['name']} deleted.")

    rule_schema = {"type": ["object", "null"], "description": "enabled, allow_senders, require_mention_in_groups; null deletes"}
    capabilities = [
        Capability("core.send_message", "Send a text message to another conversation (interface:conversation).",
                   guarded(send_message), {"address": {"type": "string"}, "text": {"type": "string"}},
                   ("address", "text"), Risk.WRITE, owner_only=True),
        Capability("core.list_rules", "List gateway admission rules by address pattern.",
                   guarded(lister(admin.rules, "rules")), risk=Risk.READ, owner_only=True),
        Capability("core.set_rule", "Set or delete (rule=null) the admission rule for an address pattern.",
                   guarded(set_rule), {"pattern": {"type": "string"}, "rule": rule_schema}, ("pattern",),
                   Risk.WRITE, owner_only=True),
        Capability("core.list_routes", "List reply routes by address pattern.",
                   guarded(lister(admin.routes, "routes")), risk=Risk.READ, owner_only=True),
        Capability("core.set_route", "Set or delete (route=null) where replies for an address pattern go. "
                   "route: {target: 'interface:conversation', kinds: ['text','file','progress','notice']}",
                   guarded(set_route), {"pattern": {"type": "string"}, "route": {"type": ["object", "null"]}},
                   ("pattern",), Risk.WRITE, owner_only=True),
        Capability("core.list_routines", "List scheduled routines.",
                   guarded(lister(admin.routines, "routines")), risk=Risk.READ, owner_only=True),
        Capability("core.set_routine", "Create or replace a routine that starts a run in routine:<name>. "
                   "Give every_minutes or daily_at (HH:MM, local time) and the instruction text.",
                   guarded(set_routine),
                   {"name": {"type": "string"}, "text": {"type": "string"},
                    "every_minutes": {"type": "number", "minimum": 1}, "daily_at": {"type": "string"},
                    "enabled": {"type": "boolean"}}, ("name", "text"), Risk.WRITE, owner_only=True),
        Capability("core.delete_routine", "Delete a routine.", guarded(delete_routine),
                   {"name": {"type": "string"}}, ("name",), Risk.WRITE, owner_only=True),
        Capability("core.list_assignments", "List engine/persona/policy assignments by address pattern.",
                   guarded(list_assignments), risk=Risk.READ, owner_only=True),
        Capability("core.set_assignment", "Create or replace the runtime assignment for an address pattern. "
                   "assignment: {engine, personas, allow, deny, preapprove, budget, trusted, lane, enabled, options}",
                   guarded(set_assignment), {"pattern": {"type": "string"}, "assignment": {"type": "object"}},
                   ("pattern", "assignment"), Risk.WRITE, owner_only=True),
        Capability("core.delete_assignment", "Delete a runtime assignment (configured ones stay).",
                   guarded(delete_assignment), {"pattern": {"type": "string"}}, ("pattern",), Risk.WRITE,
                   owner_only=True),
        Capability("core.list_forwards", "List conversations whose owner messages are forwarded elsewhere.",
                   guarded(lister(admin.forwards, "forwards")), risk=Risk.READ, owner_only=True),
        Capability("core.set_forward", "Forward the owner's ordinary messages in one conversation (exact address) "
                   "to another (target), or remove it (target=null). The target must be admitted.",
                   guarded(set_forward), {"address": {"type": "string"}, "target": {"type": ["string", "null"]}},
                   ("address",), Risk.WRITE, owner_only=True),
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
                      command_argument=operation.command_argument, timeout_s=operation.timeout_s)

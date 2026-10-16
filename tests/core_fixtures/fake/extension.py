"""Test interface: records deliveries; tests submit requests through `say`."""

import asyncio
import itertools
import time

from contracts import (Address, Contributions, Delivery, Display, Extension, Interface, Operation, Reply, Request,
                       Risk, Sender, ToolResult)

_ids = itertools.count(1)


class FakeInterface(Interface):
    def __init__(self, name, display, fail_delivery=False, delay=0.0):
        self.name = name
        self.display = display
        self.delay = delay
        self.operations = (Operation("echo", "Echo arguments back", {"value": {"type": "string"}}, ("value",),
                                     command="echoop" if name == "chat" else None,
                                     command_argument="value" if name == "chat" else None),
                           Operation("reset", "Pretend to reset", risk=Risk.WRITE))
        self.delivered: list[Reply] = []
        self.port = None
        self.started = False
        self.fail_delivery = fail_delivery

    async def start(self, port):
        self.port, self.started = port, True

    async def stop(self):
        self.started = False

    async def deliver(self, reply):
        if self.fail_delivery:
            raise ConnectionError("platform down")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.delivered.append(reply)
        return Delivery(True, message_id=f"{self.name}-out-{next(_ids)}")

    async def operate(self, operation, arguments, context=None):
        self.last_context = context
        if operation == "echo":
            return ToolResult.ok(f"echo {arguments['value']}")
        if operation == "reset":
            return ToolResult.ok("reset done")
        return await super().operate(operation, arguments, context)

    async def say(self, text, conversation="room", sender="alice", request_id=None, is_direct=True,
                  mentions_me=None, name="Alice", reply_to=None, kind="text", quoted_text="", received_at=None):
        return await self.port.submit(Request(
            id=request_id or f"in-{next(_ids)}", address=Address(self.name, conversation),
            sender=Sender(sender, name), text=text, received_at=received_at or time.time(), is_direct=is_direct,
            mentions_me=mentions_me, display=self.display, reply_to=reply_to, kind=kind, quoted_text=quoted_text))

    def texts(self, conversation=None, kind=None):
        return [reply.text for reply in self.delivered
                if (conversation is None or reply.address.conversation == conversation)
                and (kind is None or reply.kind == kind)]

    def notices(self, code=None):
        return [reply for reply in self.delivered if reply.kind == "notice" and (code is None or reply.code == code)]


def setup(context):
    config = context.config
    interfaces = []
    for name, options in (config.get("interfaces") or {"fake": {}}).items():
        options = dict(options)
        fail = options.pop("fail_delivery", False)
        delay = options.pop("delay", 0.0)
        interfaces.append(FakeInterface(name, Display(**options), fail, delay))
    return Contributions(interfaces=interfaces)


EXTENSION = Extension("fake", setup, contracts="1")

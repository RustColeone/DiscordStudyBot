"""Test capabilities. Side effects are counted in a file so restarts cannot hide double execution."""

import asyncio
import json
from pathlib import Path

from contracts import Capability, Contributions, Extension, FileRef, Persona, Risk, ToolResult

COUNTER: Path | None = None


def _count(name):
    data = json.loads(COUNTER.read_text()) if COUNTER.exists() else {}
    data[name] = data.get(name, 0) + 1
    COUNTER.write_text(json.dumps(data))
    return data[name]


async def read_value(arguments, context):
    _count("read_value")
    return ToolResult.ok(f"value={context.storage.get(arguments['key'])}")


async def write_value(arguments, context):
    count = _count("write_value")
    context.storage.set(arguments["key"], arguments["value"])
    return ToolResult.ok(f"wrote {arguments['key']} (execution {count})")


async def make_file(arguments, context):
    _count("make_file")
    path = context.workspace / arguments["name"]
    path.write_text("payload")
    await context.progress("file ready")
    return ToolResult.ok("made file", files=(FileRef(str(path), arguments["name"]),))


SLOW_RELEASE = asyncio.Event()


async def slow(arguments, context):
    _count("slow")
    await SLOW_RELEASE.wait()
    return ToolResult.ok("converted video")


async def secret(arguments, context):
    return ToolResult.ok("owner secret")


async def broken(arguments, context):
    raise TimeoutError("upstream slow")


async def not_a_result(arguments, context):
    return "plain string"


def setup(context):
    global COUNTER
    COUNTER = Path(context.config.get("counter") or context.data_dir / "counter.json")
    return Contributions(personas=[Persona("toolbot", "Toolbot", "Default persona shipped by an extension."),
                                   Persona("p", "Shadowed", "Config wins over this one.")], capabilities=[
        Capability("tools.read_value", "Read a stored value", read_value, {"key": {"type": "string"}}, ("key",)),
        Capability("tools.write_value", "Store a value", write_value,
                   {"key": {"type": "string"}, "value": {"type": "string"}}, ("key", "value"), Risk.WRITE,
                   command="setvalue", command_argument="value"),
        Capability("tools.make_file", "Create a file", make_file, {"name": {"type": "string"}}, ("name",),
                   Risk.PREPARE, needs={"files"}),
        Capability("tools.secret", "Owner-only data", secret, owner_only=True),
        Capability("tools.slow", "A long conversion", slow, risk=Risk.PREPARE),
        Capability("tools.broken", "Always times out", broken),
        Capability("tools.bad_result", "Returns the wrong type", not_a_result),
        Capability("tools.hidden", "Internal only", secret, internal=True),
    ])


EXTENSION = Extension("tools", setup, contracts="1")

"""Capabilities exercising every permission attribute, for tests/test_core_permissions.py."""

import os

from contracts import Capability, Contributions, Extension, Risk, ToolResult

CALLS: list[dict] = []  # the arguments each capability really received


def ok(text):
    async def run(arguments, context):
        CALLS.append(dict(arguments))
        return ToolResult.ok(text.format(**arguments))
    return run


async def leak(arguments, context):
    return ToolResult.ok(f"token is {os.environ.get('GUARDED_TEST_TOKEN', '')}")


def setup(context):
    return Contributions(capabilities=[
        Capability("guarded.restart", "Restart a unit", ok("restarted {unit}"), {"unit": {"type": "string"}},
                   ("unit",), Risk.CRITICAL, access="creator", subject="unit", grantable=True,
                   describe=lambda a: f"Restart {a['unit']}"),
        Capability("guarded.stop", "Stop a unit", ok("stopped {unit}"), {"unit": {"type": "string"}}, ("unit",),
                   Risk.CRITICAL, access="creator", subject="unit", confirm=True, command="gstop",
                   command_argument="unit"),
        Capability("guarded.policy", "Change policy", ok("policy changed"), risk=Risk.CRITICAL, access="creator",
                   never_auto=True),
        Capability("guarded.look", "Look at the owner's screen", ok("private view"), access="creator", private=True),
        Capability("guarded.read_file", "Read a file", ok("read {path}"), {"path": {"type": "string"}}, ("path",),
                   subject="path", subject_kind="path"),
        Capability("guarded.note", "Keep a note", ok("noted {text}"), {"text": {"type": "string"}}, ("text",),
                   Risk.WRITE),
        Capability("guarded.fetch", "Fetch something", ok("fetched")),
        Capability("guarded.leak", "Report a token", leak),
    ], permissions=dict(context.config.get("suggest") or {}))


EXTENSION = Extension("guarded", setup, contracts="1")

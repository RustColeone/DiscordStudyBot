"""Terminal: a shell on this machine, for the bot's creator only.

Three ways in, one shell per conversation:
* `$t <line>` in any conversation sends a line (`$t` alone shows new output, `$t ^C` interrupts);
* a conversation assigned to the `terminal` engine (`persona: terminal`) sends every message there to
  the shell;
* models, in runs the creator started, call `terminal.send` (each line waits for the creator's
  approval) and `terminal.peek`.
After each input the output is shown as it arrives (every few seconds), then the last screenful once
it goes quiet. Loads with the built-in modules; exclude it in core.yml to turn it off. See
docs/core/EXTENSIONS.md.
"""

from __future__ import annotations

import atexit
import platform
from pathlib import Path

from contracts import (Capability, Command, Contributions, Engine, EngineSpec, Extension, Setting, Option, Persona, Risk,
                       RunYield, ToolResult)

from .shell import INTERRUPT, Terminals


def speaks_for_creator(request) -> bool:
    """False for a routine a model set up: the core marks its firings `extra["routine_via"] == "tool"`."""
    return not (request.origin == "routine" and request.extra.get("routine_via") == "tool")


class TerminalEngine(Engine):
    spec = EngineSpec("terminal", "Every message is a line for this machine's shell (creator only).")

    def __init__(self, terminals: Terminals):
        self.terminals = terminals

    async def run(self, session) -> RunYield:
        if session.resumed:
            # A line is never sent twice: the restart may already have run it.
            await session.reply("The bot restarted while this was running; it was not sent again.")
            return RunYield.DONE
        if session.access != "creator":
            await session.reply("Only the bot's creator can use the terminal.")
            return RunYield.DONE
        if not speaks_for_creator(session.trigger):
            await session.reply("A routine set up through a tool cannot type into the terminal; type `$routine` "
                                "yourself for that.")
            return RunYield.DONE
        starter = session.trigger.sender.id

        def more():
            return [request.text for request in session.inbox()
                    if request.sender.id == starter and speaks_for_creator(request)]
        output = await self.terminals.exchange(str(session.address), session.trigger.text, session.progress, more,
                                               session.display.markdown, session.display.max_text_chars)
        await session.reply(output)
        return RunYield.DONE


def setup(context) -> Contributions:
    config = context.config
    shell = config.get("shell")
    terminals = Terminals(
        argv=[shell] if isinstance(shell, str) else shell, cwd=Path(config["cwd"]).expanduser() if config.get("cwd") else None,
        quiet_s=float(config.get("quiet_s", 1.5)), max_wait_s=float(config.get("max_wait_s", 20)),
        update_s=float(config.get("update_s", 3)), idle_s=float(config.get("idle_minutes", 60)) * 60,
        tail_chars=int(config.get("tail_chars", 1800)))
    atexit.register(terminals.kill_all)

    async def send(arguments, tool) -> ToolResult:
        text = str(arguments.get("input") or "")
        context.log("terminal_input", address=str(tool.address), chars=len(text))
        output = await terminals.exchange(str(tool.address), text, tool.progress, None, tool.display.markdown,
                                          tool.display.max_text_chars)
        return ToolResult.ok(output)

    async def peek(arguments, tool) -> ToolResult:
        output = await terminals.exchange(str(tool.address), "", tool.progress, None, tool.display.markdown,
                                          tool.display.max_text_chars)
        return ToolResult.ok(output)

    system = f"{platform.system()} ({Path(terminals.argv[0]).name})"
    command = Command("t", "Type a line into this machine's shell (creator only)",
                      (Option("input", f"A shell line; empty shows new output, {INTERRUPT} interrupts"),),
                      examples=("ls", INTERRUPT, ""), access="creator")
    return Contributions(engines=[TerminalEngine(terminals)], personas=[
        # Engines need a persona; this one only names the speaker (no model is used).
        Persona("terminal", "Terminal", "A shell on the bot's machine; no model is involved."),
    ], capabilities=[
        # Offered to models in the creator's own runs; every line still waits for the creator's approval.
        Capability("terminal.send", f"Type one line into the shell on the bot's own computer, {system}, and get "
                   "its output (the last screenful once it goes quiet). The shell belongs to this conversation and "
                   "keeps its directory and variables between lines. It has no terminal: no full-screen programs, "
                   f"no password prompts. '{INTERRUPT}' interrupts the running program. The owner approves each "
                   "line, so send one purposeful command at a time and say why.", send,
                   {"input": {"type": "string", "description": f"One shell line, or {INTERRUPT}"}},
                   risk=Risk.WRITE, access="creator", command=command, command_argument="input",
                   timeout_s=float(config.get("max_wait_s", 20)) + 30),
        Capability("terminal.peek", "Show output this conversation's shell printed since it was last shown (for "
                   "a command still running). Sends nothing.", peek, risk=Risk.READ, access="creator",
                   timeout_s=30),
    ])


EXTENSION = Extension("terminal", setup, contracts="1", version="1",
                      description="A shell on this machine for the creator (Windows and Linux)", settings=(
    Setting("cwd", "Folder new shells start in", "path", "~"),
    Setting("max_wait_s", "Longest wait for a command's output before replying, in seconds", "number", 20),
    Setting("idle_minutes", "Minutes without input before a shell ends", "number", 60),
))

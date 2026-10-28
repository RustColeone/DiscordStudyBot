"""Deterministic test engine driven by the trigger text.

  ask                 reply a question and wait for input; on the answer reply "got <answer>"
  tool <name> <json>  call a capability with call_id "t1"; reply its summary; wait for approval if needed
  tool2 <name> <json> call the same capability twice without call_id (signature dedupe)
  inbox               wait until the inbox has a message, reply "inbox <text>" for each
  hold                block until released through RELEASE (tests restarts mid-run)
  slow                report progress, then block until RELEASE
  crash               raise
  bad                 return an invalid yield
  models <n>          make n model calls
  spend <n>           make n model calls, keeping count in the state (a resumed run goes on from there)
  echo <text>         reply the text
"""

import asyncio
import json

from contracts import Contributions, Engine, EngineSpec, Extension, ModelRequest, RunYield

RELEASE = asyncio.Event()
RUNS: list[dict] = []


class ProbeEngine(Engine):
    spec = EngineSpec("probe", "test engine", "one")

    async def run(self, session):
        RUNS.append({"run": session.run_id, "resumed": session.resumed,
                     "events": [event.kind for event in session.events]})
        command, _, rest = session.trigger.text.partition(" ")
        state = dict(session.state)
        if command == "ask":
            inputs = [event for event in session.events if event.kind == "input"]
            if len(inputs) < 2:
                await session.reply("which one?")
                return RunYield.WAIT_INPUT
            await session.reply("got " + inputs[-1].data["request"]["text"])
            return RunYield.DONE
        if command in ("tool", "tool2"):
            name, _, arguments = rest.partition(" ")
            arguments = json.loads(arguments or "{}")
            for event in session.events:
                if event.kind == "tool_result":
                    await session.reply("approved: " + event.data["result"]["summary"])
                    return RunYield.DONE
                if event.kind == "approval_denied":
                    await session.reply("denied")
                    return RunYield.DONE
            if command == "tool2":
                first = await session.tools.call(name, arguments)
                second = await session.tools.call(name, arguments)
                await session.reply(f"{first.status}|{second.status}|{second.data.get('already_completed', False)}")
                return RunYield.DONE
            result = await session.tools.call(name, arguments, call_id="t1")
            if result.status == "needs_approval":
                state["waiting"] = result.data["approval_id"]
                await session.save(state)
                return RunYield.WAIT_APPROVAL
            await session.reply(f"{result.status}: {result.summary}")
            for file in result.files:
                await session.send_file(file)
            return RunYield.DONE
        if command == "inbox":
            for _ in range(200):
                items = session.inbox()
                if items:
                    for item in items:
                        await session.reply("inbox " + item.text)
                    return RunYield.DONE
                await asyncio.sleep(0.01)
            return RunYield.DONE
        if command == "hold":
            state["held"] = state.get("held", 0) + 1
            await session.save(state)
            await RELEASE.wait()
            await session.reply(f"released after {state['held']} holds")
            return RunYield.DONE
        if command == "crash":
            raise RuntimeError("engine exploded")
        if command == "bad":
            return "nonsense"
        if command == "models":
            for _ in range(int(rest)):
                await session.models.generate(ModelRequest([{"role": "user", "content": "hi"}]))
            await session.reply("models done")
            return RunYield.DONE
        if command == "spend":  # `spend <n>`: n model calls, each kept in the run's state (resumed, it goes on)
            for index in range(int(state.get("spent", 0)), int(rest)):
                await session.models.generate(ModelRequest([{"role": "user", "content": "hi"}]))
                state["spent"] = index + 1
                await session.save(state)
            await session.reply("spent")
            return RunYield.DONE
        if command == "speak":  # `speak [persona]`: one model call for (and a reply as) that persona, if named
            name = rest.strip() or None
            result = await session.models.generate(ModelRequest([{"role": "user", "content": "hi"}], persona=name))
            await session.reply(result.text, persona=name)
            return RunYield.DONE
        if command == "activity":
            await session.reply(json.dumps([[a.status, a.doing, a.request, a.sender_id] for a in session.activity()]))
            return RunYield.DONE
        if command == "options":
            await session.reply(json.dumps(dict(session.options), sort_keys=True) + "|" + session.personas[0].name)
            return RunYield.DONE
        if command == "facts":
            trigger = session.trigger
            await session.reply(f"{trigger.kind}|{trigger.quoted_text}|{trigger.reply_to}")
            return RunYield.DONE
        if command == "progress":
            await session.progress("working")
            await session.reply("done")
            return RunYield.DONE
        if command == "slow":
            await session.progress("working")
            await RELEASE.wait()
            await session.reply("slow done")
            return RunYield.DONE
        await session.reply(rest if command == "echo" else session.trigger.text)
        return RunYield.DONE


class ProbeManyEngine(ProbeEngine):
    """The probe with several personas (`speak <persona>` speaks as one of them)."""

    spec = EngineSpec("probe_many", "test engine for several personas", "many")


def setup(context):
    return Contributions(engines=[ProbeEngine(), ProbeManyEngine()])


EXTENSION = Extension("probe", setup, contracts="1")

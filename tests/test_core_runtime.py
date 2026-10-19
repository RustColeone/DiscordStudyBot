import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from contracts import Address, Request, Sender
from tests.core_support import FakeAdapter, extension_module, iface, settle, start_core


class CoreRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, overrides=None, adapter=None, clock=None):
        core = await start_core(self.tmp, overrides, adapter, clock)
        self.cores.append(core)
        probe = extension_module(core, "probe")
        probe.RELEASE = asyncio.Event()
        probe.RUNS.clear()
        return core

    async def restart(self, core, overrides=None, adapter=None):
        await core.stop()
        self.cores.remove(core)
        return await self.core(overrides, adapter)

    def counts(self):
        path = self.tmp / "counter.json"
        return json.loads(path.read_text()) if path.exists() else {}

    # ---- basic flow, admission and dedupe ----------------------------------------------------------
    async def test_message_runs_engine_reply_and_history_in_both_directions(self):
        core = await self.core()
        chat = iface(core)
        self.assertTrue(await chat.say("echo hello"))
        await settle(core)
        self.assertEqual(chat.texts("room", "text"), ["hello"])
        history = core.tank.recent_messages("chat:room")
        self.assertEqual([(item.direction, item.text) for item in history], [("in", "echo hello"), ("out", "hello")])
        self.assertEqual(core.tank.runs(("done",))[0].status, "done")

    async def test_duplicate_request_ids_are_dropped_including_after_restart(self):
        core = await self.core()
        self.assertTrue(await iface(core).say("echo one", request_id="native-1"))
        self.assertFalse(await iface(core).say("echo again", request_id="native-1"))
        await settle(core)
        core = await self.restart(core)
        self.assertFalse(await iface(core).say("echo third", request_id="native-1"))
        await settle(core)
        self.assertEqual(len(core.tank.runs(("done",))), 1)

    async def test_admission_requires_explicit_group_mention_and_respects_rules(self):
        core = await self.core({"admission": {"chat:quiet": {"enabled": False},
                                              "chat:vip": {"allow_senders": ["carol"]}}})
        chat = iface(core)
        self.assertFalse(await chat.say("echo x", is_direct=False, mentions_me=None))
        self.assertFalse(await chat.say("echo x", is_direct=False, mentions_me=False))
        self.assertTrue(await chat.say("echo yes", is_direct=False, mentions_me=True))
        self.assertFalse(await chat.say("echo x", conversation="quiet"))
        self.assertFalse(await chat.say("echo x", conversation="vip", sender="alice"))
        self.assertTrue(await chat.say("echo vip", conversation="vip", sender="carol"))
        await settle(core)
        # The owner's commands bypass admission (for example $approve in a group).
        self.assertTrue(await chat.say("$runs", conversation="quiet", sender="boss"))
        await settle(core)
        self.assertEqual(chat.texts("quiet", "text"), ["No open runs."])
        self.assertIn("yes", chat.texts("room"))
        self.assertIn("vip", chat.texts("vip"))
        decisions = [event["decision"] for event in core.audit.events if event["event"] == "admission"]
        self.assertEqual(decisions.count("not_mentioned"), 2)
        self.assertIn("disabled", decisions)
        self.assertIn("sender_not_allowed", decisions)

    async def test_interface_cannot_submit_for_another_interface_or_as_routine(self):
        core = await self.core()
        chat = iface(core)
        with self.assertRaises(ValueError):
            await chat.port.submit(Request("x", Address("owner", "dm"), Sender("boss"), "hi", time.time()))
        with self.assertRaises(ValueError):
            await chat.port.submit(Request("y", Address("chat", "room"), Sender("boss"), "hi", time.time(),
                                           origin="routine"))

    # ---- run lifecycle ----------------------------------------------------------------------------
    async def test_question_waits_for_the_next_message_and_resumes_the_same_run(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("ask")
        await settle(core)
        run = core.tank.runs(("waiting_input",))[0]
        await chat.say("the blue one")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["which one?", "got the blue one"])
        self.assertEqual(core.tank.run(run.id).status, "done")
        self.assertEqual(len(core.tank.runs(("done",))), 1)

    async def test_messages_during_a_run_go_to_its_inbox(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("inbox")
        await asyncio.sleep(0.03)
        await chat.say("follow-up")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["inbox follow-up"])
        self.assertEqual(len(core.tank.runs(("done",))), 1)

    async def test_unread_inbox_after_a_run_starts_a_new_run(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("hold")
        await asyncio.sleep(0.01)
        await chat.say("echo later")
        extension_module(core, "probe").RELEASE.set()
        await settle(core)
        self.assertEqual(chat.texts("room"), ["released after 1 holds", "later"])
        self.assertEqual(len(core.tank.runs(("done",))), 2)

    async def test_engine_failures_notify_conversation_and_owner_without_raw_text(self):
        core = await self.core()
        chat, owner = iface(core), iface(core, "owner")
        await chat.say("crash")
        await chat.say("bad", conversation="other")
        await settle(core)
        codes = [notice.data["error_code"] for notice in chat.notices("run_failed")]
        self.assertEqual(sorted(codes), ["engine_error", "engine_protocol"])
        self.assertFalse([text for text in chat.texts() if "exploded" in text])
        details = owner.notices("run_failed_detail")
        self.assertTrue(any("engine exploded" in notice.text for notice in details))

    async def test_running_run_resumes_after_restart(self):
        core = await self.core()
        await iface(core).say("hold")
        await asyncio.sleep(0.02)
        run = core.tank.runs(("running",))[0]
        core = await self.restart(core)
        probe = extension_module(core, "probe")
        await asyncio.sleep(0.02)
        self.assertEqual(core.tank.run(run.id).status, "running")
        self.assertTrue(probe.RUNS[-1]["resumed"])
        self.assertIn("resumed", probe.RUNS[-1]["events"])
        probe.RELEASE.set()
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["released after 2 holds"])

    async def test_model_call_budget_fails_the_run(self):
        adapter = FakeAdapter(handler=lambda messages, schema: "ok")
        core = await self.core({"assignments": {"*": {"budget": {"model_calls": 2}}}}, adapter)
        await iface(core).say("models 2")
        await iface(core).say("models 3", conversation="other")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["models done"])
        self.assertEqual(iface(core).notices("run_failed")[0].data["error_code"], "budget_exceeded")
        self.assertEqual(len(adapter.calls), 4)

    # ---- tool policy ------------------------------------------------------------------------------
    async def test_tool_policy_refusals_are_results_not_exceptions(self):
        core = await self.core({"permissions": {"chat:limited": {"use": ["tools.*", "!tools.broken"]}},
                                "extension_config": {"fake": {"interfaces": {"nofiles": {"files": False}}}}})
        chat = iface(core)
        await chat.say('tool tools.nope {}')
        await chat.say('tool basic.now {}', conversation="limited")
        await chat.say('tool tools.broken {}', conversation="limited2")
        await chat.say('tool tools.secret {}', conversation="s1")
        await chat.say('tool tools.secret {}', conversation="s2", sender="boss")
        await chat.say('tool tools.read_value {"wrong": 1}', conversation="args")
        await chat.say('tool tools.bad_result {}', conversation="bad")
        await iface(core, "nofiles").say('tool tools.make_file {"name": "a.txt"}')
        await settle(core)
        texts = chat.texts()
        self.assertTrue(texts[0].startswith("failed: No capability"))
        self.assertTrue(chat.texts("limited")[0].startswith("failed: basic.now is not allowed"))
        self.assertTrue(chat.texts("limited2")[0].startswith("failed: tools.broken failed: TimeoutError"))
        self.assertIn("only to the owner", chat.texts("s1")[0])
        self.assertEqual(chat.texts("s2"), ["success: owner secret"])
        self.assertIn("Invalid arguments", chat.texts("args")[0])
        self.assertIn("instead of a ToolResult", chat.texts("bad")[0])
        self.assertIn("needs ['files']", iface(core, "nofiles").texts()[0])

    async def test_tool_list_hides_internal_and_refused_capabilities(self):
        core = await self.core()
        await iface(core).say("hold")
        await asyncio.sleep(0.01)
        run = core.tank.runs(("running",))[0]
        names = [tool["name"] for tool in core.manager.tool_schemas(run.id)]
        self.assertIn("tools.read_value", names)
        self.assertNotIn("tools.hidden", names)
        self.assertNotIn("tools.secret", names)
        self.assertNotIn("core.send_message", names)
        extension_module(core, "probe").RELEASE.set()
        await settle(core)

    async def test_prepared_files_are_checked_against_the_conversation_limit(self):
        core = await self.core()
        chat = iface(core)
        await chat.say('tool tools.make_file {"name": "out.txt"}')
        await settle(core)
        files = [reply for reply in chat.delivered if reply.kind == "file"]
        self.assertEqual(files[0].file.name, "out.txt")
        self.assertIn("file ready", chat.texts(kind="progress"))
        big = core.config.data_dir / "big.bin"
        big.write_bytes(b"x" * (2 * 1024 * 1024))
        run = core.tank.runs(("done",))[0]
        from contracts import FileRef
        delivery = await core.manager.send_file(run.id, FileRef(str(big)))
        self.assertEqual(delivery.detail, "too_large")

    async def test_identical_write_in_one_run_executes_once(self):
        core = await self.core({"permissions": {"*": {"rules": {"tools.write_value": "auto"}}}})
        await iface(core).say('tool2 tools.write_value {"key": "k", "value": "v"}')
        await iface(core).say('tool2 tools.read_value {"key": "k"}', conversation="reads")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["success|success|True"])
        self.assertEqual(iface(core).texts("reads"), ["success|success|False"])
        self.assertEqual(self.counts(), {"write_value": 1, "read_value": 2})

    # ---- approvals --------------------------------------------------------------------------------
    async def test_write_waits_for_owner_approval_then_runs_exactly_once(self):
        core = await self.core()
        chat, owner = iface(core), iface(core, "owner")
        await chat.say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        run = core.tank.runs(("waiting_approval",))[0]
        request = owner.notices("approval_request")[0]
        approval_id = request.data["approval_id"]
        self.assertEqual(json.loads(request.data["arguments"]), {"key": "k", "value": "v"})
        self.assertEqual(self.counts(), {})
        await chat.say(f"$approve {approval_id}", conversation="room", sender="alice")
        await settle(core)
        self.assertIn("Only the owner can decide approvals.", chat.texts("room"))
        await owner.say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await owner.say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.counts(), {"write_value": 1})
        self.assertEqual(core.tank.run(run.id).status, "done")
        self.assertIn("approved: wrote k (execution 1)", chat.texts("room"))
        self.assertEqual(owner.texts("dm")[-2:], [f"Approved {approval_id}: tools.write_value → success.",
                                                  f"Approval {approval_id} is not pending."])

    async def test_denied_approval_resumes_engine_without_running(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        await iface(core, "owner").say(f"$deny {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["denied"])
        self.assertEqual(self.counts(), {})

    async def test_pending_approval_survives_restart_and_executes_once(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        core = await self.restart(core)
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.counts(), {"write_value": 1})
        self.assertIn("approved: wrote k (execution 1)", iface(core).texts("room"))

    async def test_interrupted_write_is_never_repeated_after_restart(self):
        core = await self.core({"permissions": {"*": {"rules": {"tools.write_value": "auto"}}}})
        await iface(core).say("hold")
        await asyncio.sleep(0.02)
        run = core.tank.runs(("running",))[0]
        # Simulate a crash after the action started but before its result was stored.
        core.tank.start_action(run.id, "call:t1", "tools.write_value", {"key": "k", "value": "v"}, "write")
        result = await core.manager.call_tool(run.id, "tools.write_value", {"key": "k", "value": "v"}, "t1")
        self.assertEqual(result.error_code, "uncertain_previous_attempt")
        again = await core.manager.call_tool(run.id, "tools.write_value", {"key": "k", "value": "v"}, "t1")
        self.assertEqual(again.error_code, "uncertain_previous_attempt")
        self.assertEqual(self.counts(), {})
        extension_module(core, "probe").RELEASE.set()
        await settle(core)

    async def crash_after_deciding(self, status="approved", action=None):
        """Ask for a write, then leave the tank as a crash during the decision would: decided, with the action
        started, finished or not reached (`action`), and no result reported to the run. Restart."""
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        approval = core.tank.approval(approval_id)
        core.tank.decide_approval(approval_id, status, "owner:dm")
        if action:
            core.tank.start_action(approval["run_id"], approval["call_key"], approval["name"],
                                   approval["arguments"], "write")
        if action == "finished":
            core.tank.finish_action(approval["run_id"], approval["call_key"], {"status": "success",
                                                                                "summary": "wrote k (before)"})
        core = await self.restart(core)
        await settle(core)
        return core, approval_id

    async def test_an_approved_write_cut_off_while_running_is_reported_uncertain_not_repeated(self):
        core, approval_id = await self.crash_after_deciding(action="started")
        self.assertEqual(self.counts(), {})  # not run again
        self.assertIn("may have taken effect; it was not repeated", iface(core).texts("room")[-1])
        self.assertEqual(core.tank.runs(("done",))[0].status, "done")
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.counts(), {})

    async def test_an_approved_write_cut_off_before_it_ran_is_not_run_without_the_owner(self):
        core, _ = await self.crash_after_deciding()
        self.assertEqual(self.counts(), {})
        self.assertIn("restart came before it ran", iface(core).texts("room")[-1])

    async def test_a_finished_write_whose_report_was_lost_reports_its_result(self):
        core, _ = await self.crash_after_deciding(action="finished")
        self.assertEqual(iface(core).texts("room")[-1], "approved: wrote k (before)")

    async def test_a_denial_whose_report_was_lost_reaches_the_run(self):
        core, _ = await self.crash_after_deciding(status="denied")
        self.assertEqual(iface(core).texts("room")[-1], "denied")

    async def test_preapproved_write_runs_without_asking(self):
        core = await self.core({"permissions": {"*": {"rules": {"tools.*": "auto"}}}})
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["success: wrote k (execution 1)"])
        self.assertFalse(iface(core, "owner").notices("approval_request"))

    async def test_without_a_control_route_only_the_owner_can_be_asked(self):
        core = await self.core({"control": None})
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}', conversation="mine", sender="boss")
        await settle(core)
        self.assertIn("No owner approval route", iface(core).texts("room")[0])
        self.assertEqual(iface(core).notices("approval_request")[0].address, Address("chat", "mine"))

    # ---- commands ---------------------------------------------------------------------------------
    async def test_commands_run_capabilities_directly_with_owner_as_approval(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("$now UTC")
        await chat.say("$setvalue v1", conversation="boss-room", sender="boss")
        await chat.say("$remember guest note", conversation="guest")
        await chat.say("$nosuch")
        await settle(core)
        self.assertIn("UTC", chat.texts("room", "text")[0])
        self.assertIn("Invalid arguments", chat.texts("boss-room")[0])
        self.assertEqual(chat.notices("waiting_approval")[0].address, Address("chat", "guest"))
        self.assertEqual(chat.notices("unknown_command")[0].data["command"], "nosuch")

    async def test_owner_command_approval_executes_and_delivers_result(self):
        core = await self.core()
        await iface(core).say("$remember buy milk", conversation="guest")
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(iface(core).texts("guest", "text"), ["Saved note #1."])
        await iface(core).say("$notes", conversation="guest")
        await settle(core)
        self.assertEqual(iface(core).texts("guest", "text")[-1], "1. buy milk")

    async def test_a_decision_cut_off_is_recorded_while_another_approval_still_waits(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        first = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        run_id = first["run_id"]
        # The same run also waits on a second approval (as parallel room jobs can).
        core.tank.create_approval("second", run_id, "call:t2", "tools.write_value", {"key": "k2", "value": "v2"},
                                  first["address"])
        core.tank.decide_approval(first["id"], "approved", "owner:dm")
        core.tank.start_action(run_id, first["call_key"], first["name"], first["arguments"], "write")
        core = await self.restart(core)
        await settle(core)
        recorded = [event.data["call_key"] for event in core.tank.events(run_id, ("tool_result",))]
        self.assertEqual(recorded, [first["call_key"]])  # the first outcome is recorded now,
        self.assertEqual(core.tank.run(run_id).status, "waiting_approval")  # but the run waits for the second
        self.assertEqual(iface(core).texts("room"), [])
        await iface(core, "owner").say("$approve second", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.counts(), {"write_value": 1})  # the second ran; the first was not repeated
        self.assertIn("may have taken effect; it was not repeated", iface(core).texts("room")[-1])
        outcomes = [(event.data["call_key"], event.data["result"]["status"])
                    for event in core.tank.events(run_id, ("tool_result",))]
        self.assertEqual(outcomes, [(first["call_key"], "failed"), ("call:t2", "success")])

    async def approve_slow(self, overrides=None):
        """A run asks to use tools.slow, which waits for SLOW_RELEASE; the owner approves it (not awaited)."""
        core = await self.core({"permissions": {"*": {"rules": {"tools.slow": "ask"}}}, **(overrides or {})})
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        await iface(core).say("tool tools.slow {}")
        await settle(core)
        approval = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        await iface(core, "owner").say(f"$approve {approval['id']}", conversation="dm", sender="boss")
        await asyncio.sleep(0.05)  # being carried out now
        return core, tools, approval

    def reported(self, core, approval):
        return [event.data["result"] for event in core.tank.events(approval["run_id"], ("tool_result",))]

    async def test_cancel_stops_an_approved_action_being_carried_out(self):
        core, tools, approval = await self.approve_slow()
        await iface(core).say("$cancel")
        await asyncio.sleep(0.05)
        tools.SLOW_RELEASE.set()  # had it gone on, it would finish now
        await settle(core)
        self.assertTrue(iface(core).texts("room")[-1].startswith("Cancelled run"))
        self.assertEqual([result.get("error_code") for result in self.reported(core, approval)], ["cancelled"])
        self.assertEqual(core.tank.action(approval["run_id"], approval["call_key"])["status"], "done")

    async def test_an_approved_action_runs_within_the_runs_time_budget(self):
        core, tools, approval = await self.approve_slow({"assignments": {"*": {"budget": {"seconds": 0.2}}}})
        await asyncio.sleep(0.4)  # longer than the run had left
        tools.SLOW_RELEASE.set()
        await settle(core)
        self.assertEqual([result.get("error_code") for result in self.reported(core, approval)], ["budget_exceeded"])
        self.assertGreaterEqual(core.tank.run(approval["run_id"]).usage["seconds"], 0.15)  # its time was counted

    async def test_approved_actions_side_by_side_count_their_time_once(self):
        core = await self.core({"permissions": {"*": {"rules": {"tools.slow": "ask"}}},
                                "assignments": {"*": {"budget": {"seconds": 0.4}}}})
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        await iface(core).say("tool tools.slow {}")
        await settle(core)
        first = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        core.tank.create_approval("second", first["run_id"], "call:t2", "tools.slow", {}, first["address"])
        owner = iface(core, "owner")
        await owner.say(f"$approve {first['id']}", conversation="dm", sender="boss")
        await owner.say("$approve second", conversation="dm", sender="boss")
        await asyncio.sleep(0.25)  # both carried out at once for about this long
        tools.SLOW_RELEASE.set()
        await settle(core)
        self.assertEqual([result["status"] for result in self.reported(core, first)], ["success", "success"])
        self.assertLess(core.tank.run(first["run_id"]).usage["seconds"], 0.4)
        self.assertEqual(core.tank.run(first["run_id"]).status, "done")  # not failed for time it did not use

    async def test_waiting_for_a_delivery_does_not_use_an_approved_actions_time(self):
        core = await self.core({"permissions": {"*": {"rules": {"tools.make_file": "ask"}}},
                                "assignments": {"*": {"budget": {"seconds": 0.2}}},
                                "extension_config": {"fake": {"interfaces": {"chat": {"delay": 0.3, "progress": True}}}}})
        await iface(core).say("$makefile notes.txt")  # a member's command, waiting for the owner
        await settle(core)
        approval = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        await iface(core, "owner").say(f"$approve {approval['id']}", conversation="dm", sender="boss")
        await settle(core)  # its progress message takes 0.3 seconds to deliver, longer than the whole budget
        self.assertEqual([result["status"] for result in self.reported(core, approval)], ["success"])
        self.assertIn("file ready", iface(core).texts("room", "progress"))

    async def test_stopping_the_core_during_an_approved_action_leaves_it_to_the_next_start(self):
        core, tools, approval = await self.approve_slow()
        core = await self.restart(core)  # a stop, not a cancel: the action is cut off, not reported cancelled
        await settle(core)
        self.assertEqual([result.get("error_code") for result in self.reported(core, approval)],
                         ["uncertain_previous_attempt"])

    async def test_a_decision_cut_off_in_a_run_kept_running_reaches_it_when_it_resumes(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        run_id = approval["run_id"]
        core.tank.update_run(run_id, status="running")  # as a room run stays while other jobs work
        core.tank.decide_approval(approval["id"], "approved", "owner:dm")
        core.tank.start_action(run_id, approval["call_key"], approval["name"], approval["arguments"], "write")
        core = await self.restart(core)
        await settle(core)
        kinds = [event.kind for event in core.tank.events(run_id) if event.kind in ("tool_result", "resumed")]
        self.assertEqual(kinds, ["tool_result", "resumed"])  # its outcome was there before it went on
        self.assertTrue(iface(core).texts("room")[-1].startswith("approved: An earlier attempt"))  # read, not redone
        self.assertEqual(self.counts(), {})

    async def asked_twice(self):
        """A run whose call call:t1 was decided and reported once, then asked again (same call key)."""
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        first = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        core.tank.decide_approval(first["id"], "approved", "owner:dm")
        core.tank.append_event(first["run_id"], "tool_result", {"call_key": first["call_key"], "name": first["name"],
                                                                "approval_id": first["id"], "arguments": first["arguments"],
                                                                "result": {"status": "failed", "summary": "earlier"}})
        core.tank.create_approval("again", first["run_id"], first["call_key"], first["name"], first["arguments"],
                                  first["address"])
        return core, first

    def outcomes(self, core, run_id):
        return [(event.data.get("approval_id"), event.data["result"]["summary"])
                for event in core.tank.events(run_id, ("tool_result",))]

    async def test_a_report_from_before_reports_named_their_approval_still_counts(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        core.tank.decide_approval(approval["id"], "approved", "owner:dm")
        core.tank.append_event(approval["run_id"], "tool_result", {"call_key": approval["call_key"], "result": {
            "status": "success", "summary": "wrote k"}})  # as decide() reported before it named the approval
        core.manager._record_decisions(core.tank.run(approval["run_id"]))
        self.assertEqual(self.outcomes(core, approval["run_id"]), [(None, "wrote k")])  # not reported twice

    async def test_a_decision_still_being_carried_out_is_left_to_it(self):
        core, first = await self.asked_twice()
        core.tank.decide_approval("again", "approved", "owner:dm")
        async with core.manager._approval_locks["again"]:  # decide() holds it while the action runs
            core.manager._record_decisions(core.tank.run(first["run_id"]))
        self.assertEqual(self.outcomes(core, first["run_id"]), [(first["id"], "earlier")])

    async def test_an_earlier_attempts_finished_action_is_not_taken_for_this_approvals(self):
        core, first = await self.asked_twice()
        core.tank.start_action(first["run_id"], first["call_key"], first["name"], first["arguments"], "write")
        core.tank.finish_action(first["run_id"], first["call_key"], {"status": "success", "summary": "old result"})
        core.tank.decide_approval("again", "approved", "owner:dm")  # then the stop came before it started
        core.manager._record_decisions(core.tank.run(first["run_id"]))
        self.assertIn("restart came before it ran", self.outcomes(core, first["run_id"])[-1][1])

    async def test_a_call_asked_again_is_recovered_on_its_own(self):
        core, first = await self.asked_twice()
        core.tank.decide_approval("again", "approved", "owner:dm")
        core.tank.start_action(first["run_id"], first["call_key"], first["name"], first["arguments"], "write")
        core = await self.restart(core)
        await settle(core)
        outcomes = [event.data["result"]["summary"] for event in core.tank.events(first["run_id"], ("tool_result",))]
        self.assertEqual(len(outcomes), 2)  # the earlier report did not count for the new approval
        self.assertIn("may have taken effect", outcomes[-1])

    async def test_a_recovered_denial_resumes_with_no_result_not_an_older_one(self):
        core, first = await self.asked_twice()
        core.tank.decide_approval("again", "denied", "owner:dm")
        self.assertIsNone(core.manager._record_decisions(core.tank.run(first["run_id"])))
        self.assertEqual(core.tank.events(first["run_id"], ("tool_result", "approval_denied"))[-1].kind,
                         "approval_denied")

    async def test_a_command_cut_off_while_its_approval_ran_is_closed_with_its_outcome(self):
        core = await self.core()
        await iface(core).say("$remember buy milk", conversation="guest")
        await settle(core)
        approval = core.tank.approval(iface(core, "owner").notices("approval_request")[0].data["approval_id"])
        core.tank.decide_approval(approval["id"], "approved", "owner:dm")
        core.tank.start_action(approval["run_id"], approval["call_key"], approval["name"], approval["arguments"],
                               "write")
        core = await self.restart(core)
        await settle(core)
        self.assertIn("may have taken effect; it was not repeated", iface(core).texts("guest", "text")[-1])
        self.assertEqual(core.tank.run(approval["run_id"]).status, "done")

    async def test_cancel_stops_a_waiting_run_and_expires_its_approval(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        await iface(core).say("$cancel")
        await settle(core)
        self.assertTrue(iface(core).texts("room")[-1].startswith("Cancelled run"))
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(iface(core, "owner").texts("dm")[-1], f"Approval {approval_id} is not pending.")
        self.assertEqual(self.counts(), {})

    async def test_a_command_cancelling_its_own_conversation_stops_the_others_and_answers(self):
        core = await self.core()
        await iface(core).say("hold")
        for _ in range(100):
            if core.tank.runs(("running",)):
                break
            await asyncio.sleep(0.01)
        held = core.tank.runs(("running",))[0]
        await iface(core).say('$call core.cancel_runs {"address": "chat:room"}', sender="boss")
        await asyncio.wait_for(settle(core), 5)
        self.assertEqual(core.tank.run(held.id).status, "cancelled")
        self.assertIn("open run(s) in chat:room", iface(core).texts("room")[-1])

    # ---- routing, routines, operations -------------------------------------------------------------
    async def test_progress_goes_to_control_when_the_conversation_hides_it(self):
        core = await self.core({"extension_config": {"fake": {"interfaces": {"quiet": {"progress": False}}}}})
        await iface(core, "quiet").say("progress")
        await settle(core)
        self.assertEqual(iface(core, "quiet").texts(), ["done"])
        progress = [reply for reply in iface(core, "owner").delivered if reply.kind == "progress"]
        self.assertEqual(progress[0].data["origin_address"], "quiet:room")

    async def test_routine_runs_unattended_and_reports_to_its_route(self):
        now = [time.time()]
        core = await self.core({
            "routes": {"routine:*": {"target": "owner:reports"}},
            "routines": {"daily": {"every_minutes": 60, "text": 'tool tools.secret {}'},
                         "trusted": {"every_minutes": 60, "text": 'tool tools.secret {}'}},
            "assignments": {"routine:trusted": {"engine": "probe", "personas": ["p"], "trusted": True}},
        }, clock=lambda: now[0])
        self.assertEqual(await core.gateway.tick(), [])
        now[0] += 3601
        self.assertEqual(sorted(await core.gateway.tick()), ["daily", "trusted"])
        self.assertEqual(await core.gateway.tick(), [])
        await settle(core)
        reports = iface(core, "owner").texts("reports")
        self.assertIn("success: owner secret", reports)
        self.assertTrue(any("only to the owner" in text for text in reports))
        run = core.tank.runs(("done",), address="routine:daily")[0]
        self.assertEqual(run.origin, "routine")

    async def test_routine_does_not_double_fire_after_a_crash_before_rescheduling(self):
        now = [time.time() + 7200]
        core = await self.core({"routines": {"r": {"every_minutes": 60, "text": "echo hi"}}}, clock=lambda: now[0])
        routine = core.bottle.routines()["r"]
        now[0] = routine["next_at"] + 1
        request_id = f"r:{int(routine['next_at'])}"
        core.bottle.mark_seen("routine:" + request_id)
        await core.gateway.tick()
        await settle(core)
        self.assertEqual(core.tank.runs(("done",), address="routine:r"), [])
        self.assertGreater(core.bottle.routines()["r"]["next_at"], routine["next_at"])

    async def test_engine_can_speak_as_its_personas_and_history_records_the_speaker(self):
        from core.manager.handles import SessionHandle
        core = await self.core({"personas": {"q": {"display_name": "Quinn", "prompt": "You are Quinn."}}})
        await iface(core).say("echo hi")
        await settle(core)
        record = core.tank.runs(("done",))[0]
        core.tank.update_run(record.id, status="running")
        session = SessionHandle(core.manager, core.tank.run(record.id), [], False)
        self.assertTrue((await session.reply("I'll look", persona="p", label="Work note")).ok)
        with self.assertRaises(ValueError):
            await session.reply("x", persona="q")
        delivered = iface(core).delivered[-1]
        self.assertEqual((delivered.speaker, delivered.label, delivered.text), ("Pat", "Work note", "I'll look"))
        latest = core.tank.recent_messages("chat:room")[-1]
        self.assertEqual((latest.sender_name, latest.kind), ("Pat", "note"))

    def test_history_keeps_attachments_and_older_databases_are_upgraded(self):
        import sqlite3
        from contracts import FileRef
        from core.storage.tank import Tank
        path = self.tmp / "old.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL, "
                           "direction TEXT NOT NULL, message_id TEXT, sender_id TEXT, sender_name TEXT, text TEXT "
                           "NOT NULL, kind TEXT NOT NULL, run_id TEXT, reply_to TEXT, quoted_text TEXT, at REAL NOT NULL)")
        connection.execute("INSERT INTO messages(address,direction,text,kind,at) VALUES('a:b','in','old','text',1)")
        connection.commit()
        connection.close()
        tank = Tank(path)
        tank.record_message("a:b", "in", "[Image]", kind="image", files=(FileRef("/x/p.png", "p.png", "image/png", 3),))
        items = tank.recent_messages("a:b")
        self.assertEqual([(item.text, item.files) for item in items],
                         [("old", ()), ("[Image]", (FileRef("/x/p.png", "p.png", "image/png", 3),))])
        tank.close()

    # ---- scheduled messages ------------------------------------------------------------------------
    async def test_scheduled_message_fires_once_when_due_and_is_recorded(self):
        now = [time.time()]
        core = await self.core(clock=lambda: now[0])
        address, alice = Address("chat", "room"), Sender("alice", "Alice")
        first = core.gateway.schedule(address, now[0] + 600, "drink water", alice)
        core.gateway.schedule(address, now[0] + 1200, "stretch", alice)
        self.assertEqual([item["text"] for item in core.gateway.scheduled_for(address)], ["drink water", "stretch"])
        await core.gateway.tick(now[0] + 599)
        await settle(core)
        self.assertEqual(iface(core).notices("reminder"), [])
        await core.gateway.tick(now[0] + 600)
        await settle(core)
        await core.gateway.tick(now[0] + 601)
        await settle(core)
        notices = iface(core).notices("reminder")
        self.assertEqual([(n.text, n.data["scheduled_id"], n.data["sender_id"]) for n in notices],
                         [("drink water", first, "alice")])
        self.assertEqual([item["text"] for item in core.gateway.scheduled_for(address)], ["stretch"])
        self.assertIn(("out", "drink water"), [(m.direction, m.text) for m in core.tank.recent_messages("chat:room")])

    async def test_scheduled_message_limits_and_cancellation(self):
        now = [time.time()]
        core = await self.core(clock=lambda: now[0])
        address, alice = Address("chat", "room"), Sender("alice", "Alice")
        for at, text in ((now[0] - 1, "past"), (now[0] + 400 * 86400, "far"), (now[0] + 60, "  ")):
            with self.assertRaises(ValueError):
                core.gateway.schedule(address, at, text, alice)
        scheduled_id = core.gateway.schedule(address, now[0] + 60, "x", alice)
        self.assertFalse(core.gateway.cancel_scheduled(Address("chat", "other"), scheduled_id))
        self.assertTrue(core.gateway.cancel_scheduled(address, scheduled_id))
        self.assertFalse(core.gateway.cancel_scheduled(address, scheduled_id))
        await core.gateway.tick(now[0] + 120)
        await settle(core)
        self.assertEqual(iface(core).notices("reminder"), [])
        core.gateway.MAX_SCHEDULED_PER_CONVERSATION = 2
        core.gateway.schedule(address, now[0] + 60, "a", alice)
        core.gateway.schedule(address, now[0] + 60, "b", alice)
        with self.assertRaises(ValueError):
            core.gateway.schedule(address, now[0] + 60, "c", alice)

    async def test_scheduled_message_waits_for_interface_and_skips_closed_conversations(self):
        now = [time.time()]
        core = await self.core({"admission": {"chat:closed": {"enabled": False}}}, clock=lambda: now[0])
        alice = Sender("alice", "Alice")
        core.gateway.schedule(Address("chat", "room"), now[0] + 60, "later", alice)
        closed = core.gateway.schedule(Address("chat", "closed"), now[0] + 60, "never", alice)
        iface(core).port.set_ready(False, "reconnecting")
        await core.gateway.tick(now[0] + 61)
        await settle(core)
        self.assertEqual(iface(core).notices("reminder"), [])
        self.assertEqual(len(core.gateway.scheduled_for(Address("chat", "room"))), 1)
        iface(core).port.set_ready(True)
        await core.gateway.tick(now[0] + 62)
        await settle(core)
        self.assertEqual([n.text for n in iface(core).notices("reminder")], ["later"])
        self.assertEqual(core.bottle.scheduled(status="skipped")[0]["id"], closed)

    async def test_a_slow_interface_does_not_stall_the_timer(self):
        now = [time.time()]
        core = await self.core({"extension_config": {"fake": {"interfaces": {"slow": {"delay": 0.5}}}}},
                               clock=lambda: now[0])
        core.gateway.schedule(Address("slow", "room"), now[0] + 60, "later", Sender("alice", "Alice"))
        started = time.monotonic()
        await core.gateway.tick(now[0] + 61)
        self.assertLess(time.monotonic() - started, 0.3)
        await settle(core)
        self.assertEqual([n.text for n in iface(core, "slow").notices("reminder")], ["later"])

    async def test_scheduled_message_interrupted_while_sending_is_never_sent_twice(self):
        core = await self.core()
        address = Address("chat", "room")
        scheduled_id = core.gateway.schedule(address, time.time() + 60, "once", Sender("alice", "Alice"))
        core.bottle.move_scheduled(scheduled_id, "firing")
        core = await self.restart(core)
        await core.gateway.tick(time.time() + 120)
        await settle(core)
        self.assertEqual(iface(core).notices("reminder"), [])
        self.assertEqual(core.bottle.scheduled(status="uncertain")[0]["id"], scheduled_id)

    async def test_interface_operations_and_core_capabilities_are_owner_only(self):
        core = await self.core({"permissions": {"*": {"rules": {"chat.reset": "auto"}}}})
        chat = iface(core)
        await chat.say('tool chat.echo {"value": "x"}', sender="boss")
        await chat.say('tool chat.echo {"value": "x"}', conversation="guest")
        await chat.say('tool chat.reset {}', conversation="boss2", sender="boss")
        await chat.say('tool core.send_message {"address": "owner:dm", "text": "hey"}', conversation="b3", sender="boss")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["success: echo x"])
        self.assertIn("only to the owner", chat.texts("guest")[0])
        self.assertEqual(chat.texts("boss2"), ["success: reset done"])
        # The owner asked in their own direct chat, so the approval comes to that chat.
        approval_id = chat.notices("approval_request")[0].data["approval_id"]
        self.assertFalse(iface(core, "owner").notices("approval_request"))
        await chat.say(f"$approve {approval_id}", conversation="b3", sender="boss")
        await settle(core)
        self.assertIn("hey", iface(core, "owner").texts("dm"))
        self.assertIn(("out", "hey"), [(item.direction, item.text) for item in core.tank.recent_messages("owner:dm")])

    async def test_interface_can_cancel_the_run_it_is_delivering_for(self):
        core = await self.core({"assignments": {"*": {"engine": "assistant", "personas": ["p"]}}},
                               FakeAdapter([{"message": "hi", "status": "complete", "actions": []}]))
        chat = iface(core)
        original, cancelled = chat.deliver, []

        async def deliver(reply):
            if reply.kind == "text" and reply.source == "run":
                cancelled.append(await chat.port.cancel(reply.address, "manual reply"))
            return await original(reply)
        chat.deliver = deliver
        await chat.say("hello")
        await asyncio.wait_for(settle(core), 10)
        self.assertEqual(cancelled, [1])
        self.assertEqual([run.status for run in core.tank.runs(("done", "cancelled", "failed"))], ["cancelled"])

    async def test_owner_reply_to_a_notification_reaches_a_chat_the_bot_does_not_answer(self):
        core = await self.core({"admission": {"chat:quiet": {"enabled": False}}})
        delivery = await iface(core).port.notify("quiet: hello?", origin=Address("chat", "quiet"))
        await iface(core, "owner").say("hi from the owner", conversation="dm", sender="boss",
                                       reply_to=delivery.message_id)
        await settle(core)
        self.assertEqual(iface(core).texts("quiet"), ["hi from the owner"])

    async def test_public_interface_operations_are_open_to_everyone(self):
        core = await self.core()
        await iface(core).say("$where", conversation="guest")
        await iface(core).say("$echoop x", conversation="guest2")
        await settle(core)
        self.assertEqual(iface(core).texts("guest"), ["here: chat:guest"])
        self.assertIn("only to the owner", iface(core).texts("guest2")[0])

    async def test_core_rule_capability_validates_and_changes_admission(self):
        core = await self.core({"permissions": {"*": {"rules": {"core.*": "auto", "core.set_rule": "auto"}}}})
        chat = iface(core)
        # Changing policy is critical and never automatic for a model, whatever the rules say...
        await chat.say('tool core.set_rule {"pattern": "chat:y", "rule": {"enabled": false}}', sender="boss",
                       conversation="model")
        await settle(core)
        self.assertEqual(chat.notices("approval_request")[0].data["tier"], "critical")
        self.assertTrue(await chat.say("echo still open", conversation="y"))
        # ...while the owner's own typed command is their approval.
        await chat.say('$call core.set_rule {"pattern": "chat:x", "rule": {"bogus": 1}}', sender="boss")
        await chat.say('$call core.set_rule {"pattern": "chat:x", "rule": {"enabled": false}}',
                       conversation="b2", sender="boss")
        await settle(core)
        self.assertIn("Unknown rule keys", chat.texts("room")[0])
        self.assertFalse(await chat.say("echo nope", conversation="x"))

    async def test_failed_interface_delivery_is_reported_as_uncertain(self):
        core = await self.core({"extension_config": {"fake": {"interfaces": {"down": {"fail_delivery": True}}}}})
        await iface(core, "down").say("echo x")
        await settle(core)
        deliveries = [event for event in core.audit.events if event["event"] == "delivery"
                      and event["address"] == "down:room"]
        self.assertTrue(deliveries and all(not item["ok"] and item["uncertain"] for item in deliveries))


if __name__ == "__main__":
    unittest.main()

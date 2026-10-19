"""Tests for the extension-facing features added after the docs-only WeChat review."""

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from contracts import Address, FileRef, Request, Sender
from tests.core_support import extension_module, iface, settle, start_core


class CoreCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, overrides=None):
        core = await start_core(self.tmp, overrides)
        self.cores.append(core)
        probe = extension_module(core, "probe")
        probe.RELEASE = asyncio.Event()
        probe.RUNS.clear()
        return core


class ExtensionApiTests(CoreCase):
    # ---- notifications and relays -----------------------------------------------------------------
    async def test_interface_events_go_to_control_and_owner_replies_are_relayed_to_origin(self):
        core = await self.core()
        chat, owner = iface(core), iface(core, "owner")
        delivery = await chat.port.notify("Alice: hello there", origin=Address("chat", "group-7"),
                                          code="incoming_message", data={"sender": "Alice"})
        self.assertTrue(delivery.ok)
        event = owner.delivered[-1]
        self.assertEqual((event.kind, event.code, event.address), ("event", "incoming_message", Address("owner", "dm")))
        self.assertEqual(event.data["origin_address"], "chat:group-7")
        # A non-owner reply to the event is a normal message, not a relay.
        await owner.say("echo not relayed", conversation="dm", sender="mallory", reply_to=delivery.message_id)
        await owner.say("on my way", conversation="dm", sender="boss", reply_to=delivery.message_id)
        await settle(core)
        relayed = [r for r in chat.delivered if r.address == Address("chat", "group-7")]
        self.assertEqual([(r.text, r.source) for r in relayed], [("on my way", "owner")])
        self.assertEqual(owner.notices("relayed")[0].data["ok"], "true")
        self.assertIn(("out", "on my way"), [(i.direction, i.text) for i in core.tank.recent_messages("chat:group-7")])
        self.assertIn("not relayed", owner.texts("dm", "text"))
        with self.assertRaises(ValueError):
            await chat.port.notify("x", origin=Address("owner", "dm"))

    async def test_event_routes_only_apply_when_they_name_the_event_kind(self):
        core = await self.core({"routes": {"chat:*": {"target": "owner:mirror", "kinds": ["event"]},
                                           "chat:all": {"target": "owner:all"}}})
        await iface(core).port.notify("one", origin=Address("chat", "room"))
        await iface(core).port.notify("two", origin=Address("chat", "all"))
        events = [(r.address.conversation, r.text) for r in iface(core, "owner").delivered if r.kind == "event"]
        self.assertEqual(events, [("mirror", "one"), ("dm", "two")])

    # ---- observed activity, cancellation and readiness ---------------------------------------------
    async def test_observe_records_history_and_cancel_stops_open_runs(self):
        core = await self.core()
        chat = iface(core)
        await chat.port.observe(Address("chat", "room"), "typed on the phone", direction="out",
                                sender=Sender("me", "Me"), message_id="native-9")
        await chat.say("hold")
        await asyncio.sleep(0.02)
        self.assertEqual(await chat.port.cancel(Address("chat", "room"), "manual reply"), 1)
        extension_module(core, "probe").RELEASE.set()
        await settle(core)
        history = core.tank.recent_messages("chat:room")
        self.assertEqual((history[0].direction, history[0].text, history[0].message_id),
                         ("out", "typed on the phone", "native-9"))
        self.assertEqual(core.tank.runs(("cancelled",))[0].error, "chat: manual reply")
        self.assertEqual(chat.texts("room", "text"), [])
        with self.assertRaises(ValueError):
            await chat.port.observe(Address("owner", "dm"), "x")

    async def test_readiness_transitions_are_reported_and_block_delivery(self):
        core = await self.core()
        chat, owner = iface(core), iface(core, "owner")
        chat.port.set_ready(True)
        chat.port.set_ready(False, "Confirm on phone")
        await chat.say("echo hi")
        await settle(core)
        self.assertEqual(chat.texts("room"), [])
        self.assertTrue(any(e["event"] == "delivery" and not e["ok"] for e in core.audit.events))
        chat.port.set_ready(True, "logged in")
        await settle(core)
        codes = [(r.code, r.data.get("detail")) for r in owner.delivered if r.kind == "event"]
        self.assertEqual(codes, [("interface_not_ready", "Confirm on phone"), ("interface_ready", "logged in")])

    # ---- lanes, live assignments, rechecks ---------------------------------------------------------
    async def test_sender_lanes_keep_group_members_requests_and_answers_apart(self):
        core = await self.core({"assignments": {"chat:group": {"engine": "probe", "personas": ["p"], "lane": "sender"}}})
        chat = iface(core)
        group = dict(conversation="group", is_direct=False, mentions_me=True)
        await chat.say("ask", sender="alice", **group)
        await settle(core)
        await chat.say("echo bob here", sender="bob", **group)
        await chat.say("the blue one", sender="alice", **group)
        await settle(core)
        self.assertEqual(chat.texts("group"), ["which one?", "bob here", "got the blue one"])
        lanes = sorted(run.lane for run in core.tank.runs(("done",)))
        self.assertEqual(lanes, ["chat:group#alice", "chat:group#bob"])

    async def test_runtime_assignments_start_from_now_and_carry_options(self):
        core = await self.core()
        chat = iface(core)
        core.manager.set_assignment("chat:special", {"engine": "probe", "persona": "toolbot",
                                                     "options": {"instructions": "be brief"}})
        await chat.say("options", conversation="special", received_at=time.time() - 60)
        await chat.say("options", conversation="special")
        await settle(core)
        self.assertEqual(chat.texts("special"), ['{"instructions": "be brief"}|toolbot'])
        core.manager.set_assignment("chat:special", {"engine": "probe", "persona": "p", "enabled": False})
        await chat.say("echo silent", conversation="special")
        await settle(core)
        self.assertEqual(len(chat.texts("special")), 1)
        self.assertEqual(core.tank.recent_messages("chat:special")[-1].text, "echo silent")
        with self.assertRaises(ValueError):
            core.manager.set_assignment("chat:x", {"engine": "probe", "personas": ["p", "toolbot"]})
        with self.assertRaises(ValueError):
            core.manager.delete_assignment("*")

    async def test_extension_personas_register_but_configuration_wins(self):
        core = await self.core()
        self.assertEqual(core.manager.personas["toolbot"].display_name, "Toolbot")
        self.assertEqual(core.manager.personas["p"].display_name, "Pat")

    async def test_run_output_is_rechecked_against_current_policy_before_sending(self):
        core = await self.core()
        chat = iface(core)
        probe = extension_module(core, "probe")
        await chat.say("hold", conversation="a")
        await asyncio.sleep(0.02)
        core.gateway.admin().set_rule("chat:a", {"enabled": False})
        probe.RELEASE.set()
        await settle(core)
        self.assertEqual(chat.texts("a"), [])
        # Revoking admission cancels the conversation's open work, so its late output is refused.
        run = core.tank.runs(("cancelled",), address="chat:a")[0]
        self.assertEqual(run.error, "not_admitted")
        self.assertTrue(any(e["event"] == "delivery_refused" and e["run"] == run.id for e in core.audit.events))
        probe.RELEASE = asyncio.Event()
        await chat.say("hold", conversation="b")
        await asyncio.sleep(0.02)
        core.manager.set_assignment("chat:b", {"engine": "probe", "persona": "p", "enabled": False})
        probe.RELEASE.set()
        await settle(core)
        self.assertEqual(chat.texts("b"), [])
        self.assertEqual(core.tank.runs(("cancelled",), address="chat:b")[0].error, "conversation_disabled")

    async def test_waiting_for_slow_delivery_does_not_use_the_time_budget(self):
        core = await self.core({"extension_config": {"fake": {"interfaces": {"slow": {"delay": 0.6}}}},
                                "assignments": {"slow:*": {"engine": "probe", "personas": ["p"],
                                                           "budget": {"seconds": 0.4}}}})
        await iface(core, "slow").say("echo uploaded")
        await settle(core)
        self.assertEqual(iface(core, "slow").texts(), ["uploaded"])
        run = core.tank.runs(("done",), address="slow:room")[0]
        self.assertLess(run.usage["seconds"], 0.4)

    # ---- admission additions and request facts ----------------------------------------------------
    async def test_rule_changes_never_admit_backlog_and_max_age_bounds_catch_up(self):
        core = await self.core({"admission": {"chat:aged": {"max_age_s": 30}}})
        chat = iface(core)
        before = time.time() - 5
        core.gateway.admin().set_rule("chat:new", {"enabled": True})
        self.assertFalse(await chat.say("echo backlog", conversation="new", received_at=before))
        self.assertTrue(await chat.say("echo fresh", conversation="new"))
        # A long-standing rule (no recent change) bounds catch-up by age instead.
        core.bottle.set_rule("chat:aged", {"max_age_s": 30, "since": 0})
        self.assertFalse(await chat.say("echo stale", conversation="aged", received_at=time.time() - 60))
        self.assertTrue(await chat.say("echo recent", conversation="aged", received_at=time.time() - 10))
        await settle(core)
        decisions = [e["decision"] for e in core.audit.events if e["event"] == "admission"]
        self.assertIn("before_rule_change", decisions)
        self.assertIn("too_old", decisions)
        # Re-applying an identical rule keeps its original start time.
        since = core.bottle.rules()["chat:new"]["since"]
        core.gateway.admin().set_rule("chat:new", {"enabled": True})
        self.assertEqual(core.bottle.rules()["chat:new"]["since"], since)

    async def test_commands_can_be_limited_to_owners(self):
        core = await self.core({"admission": {"chat:*": {"commands": "owners"}}})
        chat = iface(core)
        await chat.say("$AAPL price?")
        await chat.say("$runs", conversation="boss", sender="boss")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["$AAPL price?"])
        self.assertFalse(chat.notices("unknown_command"))
        self.assertEqual(len(chat.texts("boss", "text")), 1)

    async def test_request_kind_quote_and_reply_link_reach_engine_and_history(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("facts", reply_to="native-1", kind="image", quoted_text="original words",
                       request_id="native-2")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["image|original words|native-1"])
        incoming, outgoing = core.tank.recent_messages("chat:room")
        self.assertEqual((incoming.kind, incoming.reply_to), ("image", "native-1"))
        self.assertEqual(outgoing.reply_to, "native-2")

    async def test_operation_commands_and_cancel_capability(self):
        core = await self.core({"permissions": {"*": {"rules": {"core.cancel_runs": "auto"}}}})
        chat = iface(core)
        await chat.say("$echoop hello", sender="boss")
        await settle(core)
        self.assertEqual(chat.texts("room", "text"), ["echo hello"])
        await chat.say("hold", conversation="busy")
        await asyncio.sleep(0.02)
        await chat.say('tool core.cancel_runs {"address": "chat:busy"}', conversation="ops", sender="boss")
        await settle(core)
        self.assertEqual(chat.texts("ops"), ["success: Cancelled 1 open run(s) in chat:busy."])
        extension_module(core, "probe").RELEASE.set()
        await settle(core)
        self.assertEqual(chat.texts("busy"), [])


if __name__ == "__main__":
    unittest.main()


class ReviewTwoTests(CoreCase):
    """Second docs-only review: commit-time checks, revocation, submit reasons, forwards, policy reads."""

    async def test_submit_reports_why_a_request_was_or_was_not_accepted(self):
        core = await self.core()
        chat = iface(core)
        self.assertEqual((await chat.say("echo a", request_id="r1")).reason, "accepted")
        self.assertEqual((await chat.say("echo a", request_id="r1")).reason, "duplicate")
        self.assertEqual((await chat.say("echo b", is_direct=False)).reason, "not_mentioned")
        self.assertEqual((await chat.say("$runs")).reason, "command")
        await settle(core)

    async def test_check_lets_an_interface_recheck_a_reply_right_before_committing(self):
        from contracts import Reply
        core = await self.core()
        chat = iface(core)
        await chat.say("hold")
        await asyncio.sleep(0.02)
        run = core.tank.runs(("running",))[0]
        reply = Reply(Address("chat", "room"), "text", "late", run_id=run.id, source="run")
        self.assertIsNone(chat.port.check(reply))
        self.assertIsNone(chat.port.check(Reply(Address("chat", "room"), "text", "owner says", source="owner")))
        await chat.say("$cancel", sender="alice")
        await settle(core)
        self.assertEqual(chat.port.check(reply), "run_closed")
        extension_module(core, "probe").RELEASE.set()
        await settle(core)

    async def test_disabling_an_assignment_cancels_work_and_expires_approvals(self):
        core = await self.core()
        await iface(core).say('tool tools.write_value {"key": "k", "value": "v"}', conversation="w")
        await settle(core)
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        core.manager.set_assignment("chat:w", {"engine": "probe", "persona": "p", "enabled": False})
        await settle(core)
        self.assertEqual(core.tank.runs(("cancelled",), address="chat:w")[0].error, "conversation_disabled")
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(iface(core, "owner").texts("dm", "text")[-1], f"Approval {approval_id} is not pending.")

    async def test_owner_forwards_respect_target_admission(self):
        core = await self.core()
        owner, chat = iface(core, "owner"), iface(core)
        core.gateway.admin().set_forward("owner:bound", "chat:friend")
        self.assertEqual((await owner.say("hello friend", conversation="bound", sender="boss")).reason, "relayed")
        await settle(core)
        self.assertEqual([(r.text, r.source) for r in chat.delivered], [("hello friend", "owner")])
        core.gateway.admin().set_rule("chat:friend", {"enabled": False})
        await owner.say("still there?", conversation="bound", sender="boss")
        await settle(core)
        self.assertEqual(len(chat.delivered), 1)
        self.assertEqual(owner.notices("relayed")[-1].data["detail"], "not_admitted")
        # Other people's messages in the bound conversation are not forwarded.
        await owner.say("echo guest", conversation="bound", sender="guest")
        await settle(core)
        self.assertEqual(owner.texts("bound", "text"), ["guest"])

    async def test_routed_run_output_respects_the_target_conversations_admission(self):
        now = [time.time()]
        core = await start_core(self.tmp, {"routes": {"routine:*": {"target": "chat:family"}},
                                           "routines": {"remind": {"every_minutes": 5, "text": "echo take medicine"}}},
                                clock=lambda: now[0])
        self.cores.append(core)
        now[0] += 301
        await core.gateway.tick()
        await settle(core)
        self.assertEqual(iface(core).texts("family"), ["take medicine"])
        core.gateway.admin().set_rule("chat:family", {"enabled": False})
        now[0] += 301
        await core.gateway.tick()
        await settle(core)
        self.assertEqual(iface(core).texts("family"), ["take medicine"])

    async def test_policy_read_observe_idempotence_cancel_all_and_operation_context(self):
        core = await self.core({"admission": {"chat:quiet": {"enabled": False}}})
        chat = iface(core)
        policy = chat.port.policy(Address("chat", "quiet"))
        self.assertFalse(policy["admission"]["enabled"])
        self.assertEqual(policy["assignment"]["engine"], "probe")
        for _ in range(2):
            await chat.port.observe(Address("chat", "room"), "phone reply", direction="out", message_id="m1",
                                    reply_to="m0", quoted_text="question")
        history = core.tank.recent_messages("chat:room")
        self.assertEqual([(h.text, h.reply_to, h.quoted_text) for h in history], [("phone reply", "m0", "question")])
        await chat.say("hold", conversation="x")
        await chat.say("hold", conversation="y")
        await asyncio.sleep(0.02)
        self.assertEqual(await chat.port.cancel_all("account changed"), 2)
        extension_module(core, "probe").RELEASE.set()
        await chat.say("$echoop hi", sender="boss")
        await settle(core)
        self.assertEqual(chat.last_context.sender.id, "boss")
        self.assertTrue(chat.last_context.workspace.is_dir())


class ReviewThreeTests(CoreCase):
    async def test_owner_can_call_any_capability_directly_with_json_arguments(self):
        core = await self.core()
        owner, chat = iface(core, "owner"), iface(core)
        await owner.say('$call core.set_rule {"pattern": "chat:vip", "rule": {"enabled": false}}',
                        conversation="dm", sender="boss")
        await owner.say("$call core.set_rule [1]", conversation="dm", sender="boss")
        await chat.say('$call core.list_rules {}')
        await settle(core)
        self.assertFalse(core.bottle.rules()["chat:vip"]["enabled"])
        texts = owner.texts("dm", "text")
        self.assertEqual(texts[0], "Admission rule for chat:vip updated.")
        self.assertIn("Invalid arguments", texts[1])
        self.assertEqual(chat.texts("room", "text"), ["Only the owner can use $call."])


class ParallelWorkTests(CoreCase):
    async def test_long_capability_does_not_hold_the_conversation(self):
        core = await self.core()
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        chat = iface(core)
        await chat.say("tool tools.slow {}", request_id="convert")
        await asyncio.sleep(0.02)
        # While the conversion runs, the same conversation is answered by a new run that can see it.
        await chat.say("activity", request_id="meanwhile")
        await chat.say("echo still here", request_id="third")
        await asyncio.sleep(0.05)
        self.assertEqual(json.loads(chat.texts("room")[0]), [["working", "tools.slow", "tool tools.slow {}", "alice"]])
        self.assertEqual(chat.texts("room")[1], "still here")
        tools.SLOW_RELEASE.set()
        await settle(core)
        self.assertEqual(chat.texts("room")[-1], "success: converted video")
        self.assertEqual(len(core.tank.runs(("done",))), 3)

    async def test_messages_while_a_run_is_thinking_still_go_to_its_inbox(self):
        core = await self.core()
        chat = iface(core)
        await chat.say("inbox")
        await asyncio.sleep(0.02)
        await chat.say("follow-up")
        await settle(core)
        self.assertEqual(chat.texts("room"), ["inbox follow-up"])

    async def test_owner_reported_by_interface_is_used_only_without_configuration(self):
        core = await self.core({"owners": {"chat": []}, "control": None})
        chat = iface(core)
        self.assertFalse(core.manager.is_owner(Request("x", Address("chat", "r"), Sender("zed"), "", 0)))
        chat.port.set_owners(["zed"], control=Address("chat", "dm-zed"))
        self.assertTrue(core.manager.is_owner(Request("x", Address("chat", "r"), Sender("zed"), "", 0)))
        self.assertEqual(core.manager.control, Address("chat", "dm-zed"))
        with self.assertRaises(ValueError):
            chat.port.set_owners(["zed"], control=Address("owner", "dm"))
        configured = await start_core(self.tmp / "configured")
        self.cores.append(configured)
        iface(configured).port.set_owners(["zed"], control=Address("chat", "dm-zed"))
        self.assertFalse(configured.manager.is_owner(Request("x", Address("chat", "r"), Sender("zed"), "", 0)))
        self.assertEqual(configured.manager.control, Address("owner", "dm"))


DROP_IN = '''
from contracts import Contributions, Display, Extension, Interface, Persona, Delivery


class Bot(Interface):
    display = Display()

    def __init__(self, name):
        self.name = name

    async def start(self, port):
        self.port = port

    async def stop(self):
        pass

    async def deliver(self, reply):
        return Delivery(True)


def setup(context):
    name = context.config["interface"]
    return Contributions(interfaces=[Bot(name)], personas=[Persona("sage", "Sage", "You are Sage.", "sage")],
                         profiles={"sage": [{"provider": "fake", "model": "big"}]},
                         assignments={name + ":*": {"engine": "probe", "personas": ["sage"]}},
                         admission={name + ":*": {"require_mention_in_groups": False}})


EXTENSION = Extension("dropin", setup, contracts="1")
'''

# A provider kind of a module's own (contracts.ModelAdapter): answers "<prefix><model>: <last message>".
ECHO_KIND = '''
from contracts import AdapterResult


class Echo:
    def __init__(self, settings):
        self.prefix = settings["prefix"]

    async def complete(self, model, messages, schema, schema_name, effort, max_tokens, timeout_s, images=()):
        return AdapterResult(f"{self.prefix}{model}: {messages[-1]['content']}")
'''


class PluginTests(CoreCase):
    def plugin(self, config: str) -> Path:
        folder = self.tmp / "plugins" / "dropin"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "extension.py").write_text(DROP_IN)
        (folder / "config.yml").write_text(config)
        return folder

    async def test_dropped_in_folder_loads_with_its_local_config_and_defaults(self):
        self.plugin("interface: sage_bot\n")
        core = await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]},
                                "assignments": {"sage_bot:vip": {"engine": "probe", "personas": ["p"]}}})
        self.assertIn("sage_bot", core.gateway.interfaces)
        self.assertEqual(core.manager.assignment_for(Address("sage_bot", "x")).personas, ("sage",))
        self.assertEqual(core.manager.assignment_for(Address("sage_bot", "vip")).personas, ("p",))
        self.assertFalse(core.gateway.rule_for(Address("sage_bot", "x"))["require_mention_in_groups"])
        self.assertIn("sage", core.providers.profiles())
        await iface(core, "sage_bot").port.submit(Request(
            id="1", address=Address("sage_bot", "x"), sender=Sender("u", "U"), text="echo hi", received_at=time.time(),
            display=iface(core, "sage_bot").display))
        await settle(core)
        self.assertEqual(core.tank.runs(("done",))[0].personas, ["sage"])

    async def test_core_yml_settings_override_the_local_config(self):
        self.plugin("interface: sage_bot\n")
        core = await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]},
                                "extension_config": {"dropin": {"interface": "renamed"}}})
        self.assertIn("renamed", core.gateway.interfaces)

    async def test_defaults_for_other_interfaces_are_refused(self):
        from core.config import ConfigError
        folder = self.plugin("interface: sage_bot\n")
        (folder / "extension.py").write_text(DROP_IN.replace('name + ":*": {"engine"', '"chat:*": {"engine"'))
        with self.assertRaisesRegex(ConfigError, "only cover its own interfaces"):
            await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]}})

    async def test_contributed_providers_serve_its_profiles_and_core_yml_settings_win(self):
        import os
        from unittest import mock
        folder = self.plugin("interface: sage_bot\n")
        (folder / "extension.py").write_text(DROP_IN.replace(
            'profiles={"sage": [{"provider": "fake", "model": "big"}]},',
            'providers={"lab": {"kind": "openai_compatible", "api_key_env": "LAB_ACCESS", "vision": True}},\n'
            '                         profiles={"sage": [{"provider": "lab", "model": "big"}]},'))
        with mock.patch.dict(os.environ, {"LAB_ACCESS": "lab-secret-value-1234"}):
            core = await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]},
                                    "providers": {"lab": {"vision": False}}})
            self.assertEqual(core.providers.configured(), ["fake", "lab"])
        self.assertIn("sage", core.providers.profiles())
        self.assertFalse(core.providers._sees({"provider": "lab"}))  # core.yml's setting wins over the module's
        self.assertIn("LAB_ACCESS", core.manager.secrets.values)  # its key is kept from models like any other

    async def test_a_module_kind_serves_its_own_providers_and_core_yml_ones(self):
        from contracts import ModelRequest
        folder = self.plugin("interface: sage_bot\n")
        (folder / "extension.py").write_text(ECHO_KIND + DROP_IN.replace(
            'profiles={"sage": [{"provider": "fake", "model": "big"}]},',
            'adapters={"echo": Echo}, providers={"lab": {"kind": "echo", "prefix": "lab/"}},\n'
            '                         profiles={"sage": [{"provider": "lab", "model": "big"}]},'))
        core = await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]},
                                "providers": {"desk": {"kind": "echo", "prefix": "desk/"}},
                                "profiles": {"desk": [{"provider": "desk", "model": "m"}]}})
        hello = [{"role": "user", "content": "hi"}]
        self.assertEqual((await core.providers.generate(ModelRequest(hello, profile="sage"))).text, "lab/big: hi")
        self.assertEqual((await core.providers.generate(ModelRequest(hello, profile="desk"))).text, "desk/m: hi")

    async def test_a_module_cannot_replace_an_existing_kind(self):
        from core.config import ConfigError
        folder = self.plugin("interface: sage_bot\n")
        (folder / "extension.py").write_text(ECHO_KIND + DROP_IN.replace(
            'profiles={"sage": [{"provider": "fake", "model": "big"}]},',
            'adapters={"openai_compatible": Echo}, profiles={"sage": [{"provider": "fake", "model": "big"}]},'))
        with self.assertRaisesRegex(ConfigError, "dropin: Provider kind 'openai_compatible' already exists"):
            await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]}})

    async def test_configured_profile_can_override_one_using_a_contributed_provider(self):
        folder = self.plugin("interface: sage_bot\n")
        (folder / "extension.py").write_text(DROP_IN.replace(
            'profiles={"sage": [{"provider": "fake", "model": "big"}]},',
            'providers={"lab": {"kind": "openai_compatible", "api_key_env": "LAB_ACCESS"}},\n'
            '                         profiles={"sage": [{"provider": "lab", "model": "big"}]},'))
        candidates = [{"provider": "lab", "model": "configured-model"}]
        core = await self.core({"modules": {"dirs": [str(self.tmp / "plugins")]},
                                "profiles": {"sage": candidates}})
        self.assertEqual(core.providers._profiles["sage"], candidates)

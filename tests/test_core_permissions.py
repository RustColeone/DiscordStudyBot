"""Permissions: Access (may it be used here), Approval (does it run without asking) and Enforcement
(secrets, private data, grants, where approvals go). See docs/core/ARCHITECTURE.md, Permissions."""

import os
import tempfile
import unittest
from pathlib import Path

from contracts import Address
from core.config import ConfigError
from core.manager.policy import Assignment, Permissions
from tests.core_support import FIXTURES, extension_module, iface, make_config, settle, start_core

SECRET = "tok-1234567890abcdef"


class PolicyTests(unittest.TestCase):
    def test_each_setting_comes_from_the_most_specific_pattern_that_sets_it(self):
        permissions = Permissions([("core.yml", {
            "*": {"use": ["*"], "tiers": {"write": "auto"}},
            "chat:*": {"tiers": {"read": "ask"}},
            "chat:x": {"use": ["basic.*"]},
            "routine:*": {"use": ["basic.*"]}})])
        x, y = Address("chat", "x"), Address("chat", "y")
        self.assertEqual(permissions.tier_setting(x, "write"), ("auto", "*"))
        self.assertEqual(permissions.tier_setting(x, "read"), ("ask", "chat:*"))
        self.assertEqual(permissions.tier_setting(Address("other", "z"), "critical"), ("ask", ""))
        self.assertEqual((permissions.allows(x, "basic.now"), permissions.allows(x, "web.search")), (True, False))
        self.assertTrue(permissions.allows(y, "web.search"))
        # An allow-list for routines keeps what it names: there is no deny to collide with.
        self.assertTrue(permissions.allows(Address("routine", "daily"), "basic.notes"))
        self.assertFalse(permissions.allows(Address("routine", "daily"), "web.search"))

    def test_rules_by_specificity_stricter_on_ties_later_layers_replace_earlier(self):
        address = Address("chat", "x")
        tie = Permissions([("core.yml", {"*": {"rules": {"g.a*": "auto", "g.ab*": "ask"}}})])
        self.assertEqual(tie.rule(address, "g.abc", None, "value", "creator", critical=False, has_subject=False)[0],
                         "ask")
        layered = Permissions([("ext", {"*": {"rules": {"g.a": "auto", "g.b": "auto"}}}),
                               ("core.yml", {"*": {"rules": {"g.a": "ask"}}})])
        decide = lambda name: layered.rule(address, name, None, "value", "member", critical=False,
                                           has_subject=False)[0]
        self.assertEqual((decide("g.a"), decide("g.b")), ("ask", "auto"))
        specific = Permissions([("core.yml", {"*": {"rules": {"g.a": "ask"}}, "chat:x": {"rules": {"g.*": "auto"}}})])
        self.assertEqual(specific.rule(address, "g.a", None, "value", "member", critical=False,
                                       has_subject=False)[0], "auto")

    def test_critical_calls_need_the_exact_name_and_subject(self):
        permissions = Permissions([("core.yml", {"*": {"rules": {
            "svc.*": "auto", "svc.restart": "auto", "svc.restart(navidrome.service)": {"creator": "auto"}}}})])
        rule = lambda subject, level="creator": permissions.rule(
            Address("chat", "x"), "svc.restart", subject, "value", level, critical=True, has_subject=True)
        self.assertEqual(rule("navidrome.service")[0], "auto")
        self.assertIsNone(rule("docker.service"))
        self.assertIsNone(rule("navidrome.service", "admin"))

    def test_invalid_permissions_are_refused(self):
        for section in ({"tiers": {"critical": "auto"}}, {"rules": {"*": "auto"}}, {"rules": {"g.a": "maybe"}},
                        {"rules": {"g.*(x)": "auto"}}, {"bogus": 1}, {"tiers": {"write": "sometimes"}}):
            with self.assertRaises(ValueError):
                Permissions([("core.yml", {"*": section})])

    def test_old_assignment_keys_are_explained_but_old_runs_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError) as caught:
                make_config(Path(directory), {"assignments": {"*": {"preapprove": ["tools.write_value"]}}})
            self.assertIn("permissions", str(caught.exception))
            with self.assertRaises(ConfigError):
                make_config(Path(directory), {"permissions": {"*": {"tiers": {"critical": "auto"}}}})
        stored = Assignment.from_dict({"pattern": "*", "engine": "probe", "personas": ["p"], "allow": ["*"],
                                       "deny": [], "preapprove": ["x.y"]})
        self.assertEqual(stored.engine, "probe")

    def test_secret_paths_include_process_environments(self):
        from core.manager.secrets import Secrets
        secrets = Secrets()
        for path in ("/proc/self/environ", "/proc/1/cmdline", "~/.ssh/id_rsa", "project/.env"):
            self.assertTrue(secrets.is_secret_path(path), path)
        self.assertFalse(secrets.is_secret_path("~/Music/song.mp3"))


class PermissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["GUARDED_TEST_TOKEN"] = SECRET
        self.core = None

    async def asyncTearDown(self):
        os.environ.pop("GUARDED_TEST_TOKEN", None)
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, overrides=None):
        if self.core is not None:
            await self.core.stop()
            self.core = None
        paths = [*make_config(self.tmp).raw["modules"]["paths"], str(FIXTURES / "guarded")]
        self.core = await start_core(self.tmp, {"extensions": paths, **(overrides or {})})
        self.chat, self.owner = iface(self.core), iface(self.core, "owner")
        extension_module(self.core, "guarded").CALLS.clear()
        return self.core

    async def say(self, text, **kwargs):
        result = await self.chat.say(text, **kwargs)
        await settle(self.core)
        return result

    async def in_group(self, text, sender="boss", conversation="group"):
        return await self.say(text, sender=sender, conversation=conversation, is_direct=False, mentions_me=True)

    def asked(self, interface=None):
        return (interface or self.chat).notices("approval_request")

    # ---- access ---------------------------------------------------------------------------------
    async def test_private_reads_run_without_asking_only_where_only_the_owner_reads(self):
        await self.start()
        await self.say("tool guarded.look {}", sender="boss", conversation="mine")
        await self.in_group("tool guarded.look {}")
        self.assertEqual(self.chat.texts("mine"), ["success: private view"])
        self.assertIn("private", self.chat.texts("group")[0])
        await self.say("$tools", sender="boss", conversation="mine")
        await self.in_group("$tools")
        self.assertIn("guarded.look", self.chat.texts("mine")[-1])
        self.assertNotIn("guarded.look", self.chat.texts("group")[-1])

    async def test_a_run_that_saw_private_data_does_not_follow_routes_elsewhere(self):
        await self.start({"routes": {"chat:sr": {"target": "chat:elsewhere"}}})
        await self.say("tool guarded.look {}", sender="boss", conversation="sr")
        await self.say("echo routed as usual", sender="boss", conversation="sr")
        self.assertEqual(self.chat.texts("elsewhere"), ["routed as usual"])
        self.assertTrue([event for event in self.core.audit.events if event["event"] == "delivery_refused"
                         and event.get("reason") == "private_route"])

    # ---- approval -------------------------------------------------------------------------------
    async def test_an_explicit_ask_is_final_even_for_a_read(self):
        await self.start({"permissions": {"*": {"rules": {"guarded.fetch": "ask"}}}})
        await self.say("tool guarded.fetch {}")
        await self.say("tool guarded.leak {}", conversation="other")
        self.assertEqual([notice.data["tier"] for notice in self.asked(self.owner)], ["read"])
        self.assertTrue(self.chat.texts("other")[0].startswith("success"))

    async def test_always_ask_has_no_exceptions_and_leaves_other_tiers_alone(self):
        await self.start({"permissions": {"chat:*": {"tiers": {"write": "always-ask"},
                                                     "rules": {"guarded.note": "auto"}}}})
        await self.say('tool guarded.note {"text": "hi"}')
        await self.say("tool guarded.fetch {}", conversation="r")
        self.assertEqual(len(self.asked(self.owner)), 1)
        self.assertEqual(self.chat.texts("r"), ["success: fetched"])

    async def test_critical_actions_run_unasked_only_for_the_exact_subject_named(self):
        await self.start({"permissions": {"*": {"rules": {
            "guarded.*": "auto", "guarded.restart": "auto", "guarded.restart(navidrome.service)": {"creator": "auto"}}}}})
        await self.say('tool guarded.restart {"unit": "navidrome.service"}', sender="boss", conversation="a")
        await self.say('tool guarded.restart {"unit": "docker.service"}', sender="boss", conversation="b")
        await self.say('tool guarded.note {"text": "x"}', conversation="c")
        self.assertEqual(self.chat.texts("a"), ["success: restarted navidrome.service"])
        self.assertEqual([notice.data["summary"] for notice in self.asked()], ["Restart docker.service"])
        self.assertEqual(self.chat.texts("c"), ["success: noted x"])

    async def test_rules_can_depend_on_the_callers_level(self):
        await self.start({"permissions": {"*": {"rules": {"guarded.note": {"creator": "auto"}}}}})
        await self.say('tool guarded.note {"text": "member"}', conversation="m")
        await self.say('tool guarded.note {"text": "owner"}', sender="boss", conversation="o")
        self.assertEqual(len(self.asked(self.owner)), 1)
        self.assertEqual(self.chat.texts("o"), ["success: noted owner"])

    async def test_never_auto_asks_whatever_the_rules_say_and_offers_no_always(self):
        await self.start({"permissions": {"*": {"rules": {"guarded.policy": "auto"}}}})
        await self.say("tool guarded.policy {}", sender="boss")
        notice = self.asked()[0]
        self.assertEqual((notice.data["tier"], notice.data["always"]), ("critical", "false"))
        self.assertEqual([action.label for action in notice.actions], ["Approve", "Deny"])

    async def test_approvals_go_to_the_owners_own_chat_otherwise_privately_to_control(self):
        await self.start()
        await self.say('tool guarded.note {"text": "a"}', conversation="alice-dm")
        await self.say('tool guarded.note {"text": "b"}', sender="boss", conversation="boss-dm")
        await self.in_group('tool guarded.note {"text": "c"}')
        await self.in_group('tool guarded.note {"text": "d"}', sender="alice", conversation="group2")
        self.assertEqual(sorted(notice.data["summary"] for notice in self.asked(self.owner)),
                         ['guarded.note {"text": "a"}', 'guarded.note {"text": "c"}', 'guarded.note {"text": "d"}'])
        self.assertEqual([(notice.address.conversation, notice.data["summary"]) for notice in self.asked()],
                         [("boss-dm", 'guarded.note {"text": "b"}')])

    # ---- typed commands -------------------------------------------------------------------------
    async def test_the_owners_typed_commands_run_except_confirmations(self):
        await self.start({"permissions": {"chat:*": {"use": ["basic.*"]}}})
        await self.say('$call guarded.restart {"unit": "x"}', sender="boss")
        self.assertIn("restarted x", self.chat.texts("room", kind="text"))
        await self.say("$gstop y", sender="boss", conversation="c2")
        notice = self.asked()[-1]
        self.assertEqual((notice.data["kind"], [action.label for action in notice.actions]),
                         ("confirm", ["Confirm", "Cancel"]))
        self.assertNotIn("stopped y", self.chat.texts("c2", kind="text"))
        await self.say(f"$approve {notice.data['approval_id']}", sender="boss", conversation="c2")
        self.assertIn("stopped y", self.chat.texts("c2", kind="text"))

    async def test_a_typed_secret_asks_to_confirm_and_only_the_scrubbed_form_is_kept(self):
        await self.start()
        await self.say('$call guarded.note {"text": "key ' + SECRET + '"}', sender="boss", conversation="s")
        notice = self.asked()[-1]
        self.assertIn("GUARDED_TEST_TOKEN", notice.data["warnings"])
        self.assertNotIn(SECRET, notice.text + notice.data["arguments"] + notice.data["summary"])
        self.assertEqual(extension_module(self.core, "guarded").CALLS, [])
        await self.say(f"$approve {notice.data['approval_id']}", sender="boss", conversation="s")
        self.assertEqual(extension_module(self.core, "guarded").CALLS, [{"text": "key " + SECRET}])
        self.assertEqual(self.core.tank.approval(notice.data["approval_id"])["arguments"],
                         {"text": "key [secret GUARDED_TEST_TOKEN]"})
        stored = [item.text for item in self.core.tank.recent_messages("chat:s", 20)]
        self.assertTrue(stored and not any(SECRET in text for text in stored))
        every = ("running", "waiting_input", "waiting_approval", "done", "failed", "cancelled")
        self.assertFalse(any(SECRET in str(run.trigger) for run in self.core.tank.runs(every, limit=50)))
        await self.say('$call guarded.note {"text": "again ' + SECRET + '"}', sender="boss", conversation="s2")
        expiring = self.asked()[-1].data["approval_id"]
        await self.say("$cancel", sender="boss", conversation="s2")
        self.assertNotIn(SECRET, str(self.core.tank.approval(expiring)["arguments"]))
        self.assertFalse(any(SECRET in text for text in self.chat.texts("s")))

    # ---- secrets --------------------------------------------------------------------------------
    async def test_models_never_send_or_read_secrets_and_results_and_replies_are_scrubbed(self):
        await self.start()
        await self.say('tool guarded.read_file {"path": "~/.ssh/id_ed25519"}', conversation="m1")
        await self.say('tool guarded.note {"text": "' + SECRET + '"}', conversation="m2")
        await self.say("tool guarded.leak {}", conversation="m3")
        await self.say("echo " + SECRET, conversation="m4")
        await self.say('tool guarded.read_file {"path": "notes.txt"}', conversation="m5")
        self.assertTrue(self.chat.texts("m1")[0].startswith("failed: Refused"))
        self.assertTrue(self.chat.texts("m2")[0].startswith("failed: Refused"))
        self.assertFalse(self.asked(self.owner))
        self.assertEqual(len(self.owner.notices("secret_refused")), 2)
        self.assertEqual(self.chat.texts("m3"), ["success: token is [secret GUARDED_TEST_TOKEN]"])
        self.assertEqual(self.chat.texts("m4"), ["[secret GUARDED_TEST_TOKEN]"])
        self.assertEqual(self.chat.texts("m5"), ["success: read notes.txt"])

    async def test_after_seeing_private_data_a_run_asks_before_anything_critical(self):
        await self.start({"permissions": {"*": {"rules": {"guarded.restart(nd)": "auto"}}}})
        await self.say("tool guarded.look {}", sender="boss", conversation="p")
        run_id = self.core.tank.runs(("done",), address="chat:p")[0].id
        result = await self.core.manager.call_tool(run_id, "guarded.restart", {"unit": "nd"}, "late")
        self.assertEqual(result.status, "needs_approval")
        notice = self.asked()[-1]
        self.assertIn("seen private data", notice.data["warnings"])
        self.assertEqual(notice.data["always"], "false")
        await self.say('tool guarded.restart {"unit": "nd"}', sender="boss", conversation="q")
        self.assertEqual(self.chat.texts("q"), ["success: restarted nd"])

    async def test_runtime_assignments_saved_with_allow_and_deny_keep_those_limits(self):
        await self.start()
        self.core.manager._overrides.set("chat:old", {"pattern": "chat:old", "engine": "probe", "personas": ["p"],
                                                      "allow": ["guarded.*"], "deny": ["guarded.leak"],
                                                      "preapprove": [], "since": 0})
        await self.start()
        allows = lambda name: self.core.manager.permissions.allows(Address("chat", "old"), name)
        self.assertEqual((allows("guarded.fetch"), allows("guarded.leak"), allows("basic.now")),
                         (True, False, False))

    # ---- grants ---------------------------------------------------------------------------------
    async def test_always_allow_needs_one_hand_approval_and_covers_one_subject_in_one_conversation(self):
        await self.start()
        restart = lambda unit="nd", conversation="g": self.say(
            'tool guarded.restart {"unit": "' + unit + '"}', sender="boss", conversation=conversation)
        await restart()
        first = self.asked()[-1]
        self.assertEqual(first.data["always"], "false")
        await self.say(f"$approve {first.data['approval_id']} always", sender="boss", conversation="g")
        self.assertIn("approved: restarted nd", self.chat.texts("g"))
        self.assertTrue(any("approved once" in text for text in self.chat.texts("g")))
        self.assertEqual(list(self.core.manager.grants.keys()), [])
        await restart()
        second = self.asked()[-1]
        self.assertEqual([action.label for action in second.actions], ["Approve", "Always allow", "Deny"])
        await self.say(f"$approve {second.data['approval_id']} always", sender="boss", conversation="g")
        self.assertEqual(len(self.core.manager.grants.keys()), 1)
        asked = len(self.asked())
        await restart()
        self.assertEqual(len(self.asked()), asked)
        self.assertEqual(self.chat.texts("g", kind="text")[-1], "success: restarted nd")
        await restart(unit="other")
        await restart(conversation="h")
        self.assertEqual(len(self.asked()), asked + 2)
        await self.say("$permissions grants", sender="boss", conversation="g")
        listing = self.chat.texts("g")[-1]
        grant_id = listing.split("\n")[1].split()[0]
        self.assertIn("guarded.restart(nd) in chat:g", listing)
        await self.say(f"$permissions revoke {grant_id}", sender="boss", conversation="g")
        await restart()
        self.assertEqual(len(self.asked()), asked + 3)

    # ---- layers and the management view ---------------------------------------------------------
    async def test_extension_defaults_then_module_config_then_core_yml(self):
        suggest = {"*": {"rules": {"guarded.note": "auto"}}}
        await self.start({"extension_config": {"guarded": {"suggest": suggest}}})
        await self.say('tool guarded.note {"text": "1"}', conversation="l1")
        self.assertEqual(self.chat.texts("l1"), ["success: noted 1"])
        module = {"suggest": suggest, "permissions": {"*": {"rules": {"guarded.note": "ask"}}}}
        await self.start({"extension_config": {"guarded": module}})
        await self.say('tool guarded.note {"text": "2"}', conversation="l2")
        self.assertEqual(len(self.asked(self.owner)), 1)
        await self.start({"extension_config": {"guarded": module},
                          "permissions": {"*": {"rules": {"guarded.note": "auto"}}}})
        await self.say('tool guarded.note {"text": "3"}', conversation="l3")
        self.assertEqual(self.chat.texts("l3"), ["success: noted 3"])

    async def test_extension_defaults_cover_only_their_own_capabilities_and_interfaces(self):
        for suggest in ({"*": {"tiers": {"write": "auto"}}}, {"*": {"rules": {"basic.remember": "auto"}}}):
            with self.assertRaises(ConfigError):
                await self.start({"extension_config": {"guarded": {"suggest": suggest}}})

    async def test_permissions_command_shows_what_applies_here_for_the_creator_only(self):
        await self.start({"permissions": {"chat:*": {"tiers": {"write": "always-ask"},
                                                     "rules": {"guarded.fetch": "ask"}}}})
        await self.say("$permissions", sender="boss", conversation="v")
        text = self.chat.texts("v")[-1]
        self.assertIn("write always-ask (chat:*)", text)
        self.assertIn("guarded.fetch: member+ ask (chat:*, core.yml)", text)
        await self.say("$permissions", conversation="v2")
        self.assertEqual([notice.code for notice in self.chat.notices() if notice.address.conversation == "v2"],
                         ["not_permitted"])


if __name__ == "__main__":
    unittest.main()

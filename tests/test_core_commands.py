"""Command declarations, the catalog and $help."""

import tempfile
import unittest
from pathlib import Path

from contracts import Action, Capability, Command, Option, ToolResult
from tests.core_support import iface, settle, start_core


class CommandDeclarationTests(unittest.TestCase):
    def test_templates_render_values_drop_empty_sections_and_flags(self):
        clip = Command("clip", "Clip", (Option("url"), Option("start"), Option("fit", type="boolean", flag="--fit")),
                       template="[-u {url}][ -s {start}][ {fit}]")
        self.assertEqual(clip.render({"url": "https://x", "fit": True}), "$clip -u https://x --fit")
        self.assertEqual(clip.render({"start": "1:05", "fit": False}), "$clip -s 1:05")
        self.assertEqual(clip.render({}), "$clip")
        plain = Command("remind", "", (Option("when"), Option("text")))
        self.assertEqual(plain.render({"text": "tea", "when": "10m"}), "$remind 10m tea")
        self.assertEqual(plain.usage(), "$remind [when] [text]")

    def test_declarations_are_validated(self):
        for bad in (lambda: Command("Bad Name"), lambda: Option("Spaces here"),
                    lambda: Command("x", options=(Option("a"),), template="{b}"),
                    lambda: Command("x", access="root")):
            with self.assertRaises(ValueError):
                bad()

    def test_a_plain_command_name_becomes_a_declaration_with_its_argument(self):
        async def run(arguments, context):
            return ToolResult.ok("")
        capability = Capability("ext.go", "Go somewhere. Then come back.", run,
                                {"place": {"type": "string", "description": "Where to"}}, ("place",),
                                command="go", command_argument="place")
        self.assertEqual(capability.command.description, "Go somewhere.")
        self.assertEqual(capability.command.options, (Option("place", "Where to", required=True),))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = await start_core(Path(self._tmp.name))

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def test_interfaces_see_the_catalog_with_access_levels(self):
        catalog = {command.name: command for command in iface(self.core).port.commands()}
        self.assertEqual(catalog["call"].access, "creator")
        self.assertEqual(catalog["help"].access, "member")
        self.assertEqual(catalog["echoop"].access, "creator")
        self.assertEqual(catalog["where"].access, "member")
        self.assertIn("remember", catalog)

    async def test_help_lists_what_the_caller_may_run_and_explains_one(self):
        chat = iface(self.core)
        await chat.say("$help")
        await chat.say("$help", conversation="boss", sender="boss")
        await chat.say("$help call", conversation="boss2", sender="boss")
        await settle(self.core)
        member, owner, detail = chat.texts("room")[0], chat.texts("boss")[0], chat.texts("boss2")[0]
        self.assertIn("$memory [args]", member)
        self.assertNotIn("$call", member)
        self.assertIn("$call <capability> [arguments]", owner)
        self.assertIn('Examples: $call core.list_rules', detail)

    async def test_approval_requests_carry_approve_and_deny_buttons(self):
        await iface(self.core).say('tool tools.write_value {"key": "k", "value": "v"}')
        await settle(self.core)
        notice = iface(self.core, "owner").notices("approval_request")[0]
        approval = notice.data["approval_id"]
        self.assertEqual(notice.actions, (Action("Approve", f"$approve {approval}", "success"),
                                          Action("Deny", f"$deny {approval}", "danger")))


if __name__ == "__main__":
    unittest.main()

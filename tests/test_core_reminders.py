"""Tests for the reminders extension inside a real core."""

import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from tests.core_support import ROOT, extension_module, iface, make_config, settle, start_core


class ReminderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        extensions = [*make_config(tmp).raw["extensions"], str(ROOT / "extensions" / "reminders")]
        self.core = await start_core(tmp, {"extensions": extensions, "assignments": {
            "*": {"engine": "probe", "personas": ["p"], "preapprove": ["reminders.create", "reminders.cancel"]}}})
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def test_remind_command_lists_and_fires_in_the_same_conversation(self):
        await self.chat.say("$remind 10m drink water")
        await self.chat.say('tool reminders.create {"text": "stretch", "in_minutes": 30}', sender="bob", name="Bob")
        await settle(self.core)
        self.assertRegex(self.chat.texts("room")[0], r"Reminder \w+ set for .*: drink water")
        await self.chat.say("$reminders")
        await settle(self.core)
        listing = self.chat.texts("room")[-1]
        self.assertLess(listing.index("drink water (Alice)"), listing.index("stretch (Bob)"))
        await self.core.gateway.tick(time.time() + 11 * 60)
        notices = self.chat.notices("reminder")
        self.assertEqual([(n.address.conversation, n.text, n.data["sender_id"]) for n in notices],
                         [("room", "drink water", "alice")])

    async def test_only_the_setter_can_cancel_and_bad_times_are_explained(self):
        await self.chat.say('tool reminders.create {"text": "call mom", "at": "%s"}'
                            % (dt.datetime.now().astimezone() + dt.timedelta(hours=2)).isoformat())
        await settle(self.core)
        reminder = self.core.gateway.scheduled_for(self.chat.delivered[0].address)[0]["id"]
        await self.chat.say(f"$unremind {reminder}", sender="mallory", name="Mallory")
        await settle(self.core)
        self.assertIn("Only the person who set", self.chat.texts("room")[-1])
        await self.chat.say(f"$unremind {reminder}")
        await self.chat.say("$remind yesterday at noon", conversation="b")
        await self.chat.say('tool reminders.create {"text": "x", "at": "2001-01-01T00:00+00:00"}', conversation="c")
        await self.chat.say('tool reminders.create {"text": "x"}', conversation="d")
        await settle(self.core)
        self.assertIn(f"Reminder {reminder} cancelled", self.chat.texts("room")[-1])
        self.assertIn("Not a time", self.chat.texts("b")[0])
        self.assertIn("in the future", self.chat.texts("c")[0])
        self.assertIn("in_minutes", self.chat.texts("d")[0])
        self.assertEqual(self.core.gateway.scheduled_for(self.chat.delivered[0].address), [])

    def test_spec_parsing(self):
        module = extension_module(self.core, "reminders")
        now = dt.datetime(2026, 10, 16, 20, 0).astimezone().timestamp()
        self.assertEqual(module.parse_spec("2h30m stretch", now), (now + 9000, "stretch"))
        self.assertEqual(module.parse_spec("45 sec tea", now), (now + 45, "tea"))
        self.assertEqual(module.parse_spec("1 day 2 hours plan", now), (now + 93600, "plan"))
        self.assertEqual(module.parse_spec("10mins", now), (now + 600, ""))
        at, text = module.parse_spec("18:30 call mom", now)
        self.assertEqual((at - now, text), (22.5 * 3600, "call mom"))
        at, text = module.parse_spec("2026-10-17T08:00+08:00 wake", now)
        self.assertEqual((dt.datetime.fromtimestamp(at, dt.timezone.utc).hour, text), (0, "wake"))


if __name__ == "__main__":
    unittest.main()

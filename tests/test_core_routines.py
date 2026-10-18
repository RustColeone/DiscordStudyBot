"""Conversation routines: schedules, `$routine`, levels, firing as the creator."""

import datetime as dt
import tempfile
import time
import unittest
from pathlib import Path

from core.gateway import routines as schedules
from tests.core_support import iface, settle, start_core


class ScheduleTests(unittest.TestCase):
    def test_parse_and_describe(self):
        now = dt.datetime(2026, 10, 16, 18, 0).astimezone().timestamp()  # a Friday
        self.assertEqual(schedules.parse("every 1h $status", now), ({"every_minutes": 60}, "$status"))
        self.assertEqual(schedules.parse("every 2h30m stretch", now), ({"every_minutes": 150}, "stretch"))
        self.assertEqual(schedules.parse("every 90 minutes drink", now), ({"every_minutes": 90}, "drink"))
        self.assertEqual(schedules.parse("daily 08:00 good morning", now), ({"daily_at": "08:00"}, "good morning"))
        schedule, _ = schedules.parse("weekdays 09:00 standup", now)
        self.assertEqual((schedule, schedules.describe(schedule)), ({"daily_at": "09:00", "days": [0, 1, 2, 3, 4]},
                                                                    "weekdays 09:00"))
        schedule, _ = schedules.parse("mon,wed 18:30 gym", now)
        self.assertEqual(schedules.describe(schedule), "mon,wed 18:30")
        once, text = schedules.parse("at 20:00 call mom", now)
        self.assertEqual((once["once_at"] - now, text), (7200, "call mom"))
        for bad in ("every soon hi", "sometimes 08:00 hi", "daily 25:00 hi", "every 1h", "at tomorrow hi"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                schedules.parse(bad, now)

    def test_next_fire_respects_days_and_one_offs(self):
        friday_evening = dt.datetime(2026, 10, 16, 18, 0).astimezone().timestamp()
        monday = dt.datetime.fromtimestamp(schedules.next_fire({"daily_at": "09:00", "days": [0, 1, 2, 3, 4]},
                                                                friday_evening)).astimezone()
        self.assertEqual((monday.weekday(), monday.hour), (0, 9))
        self.assertIsNone(schedules.next_fire({"once_at": friday_evening - 1}, friday_evening))


class RoutineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.now = [time.time()]
        self.core = await start_core(Path(self._tmp.name), {
            "assignments": {"*": {"engine": "probe", "personas": ["p"]}},
            "permissions": {"*": {"rules": {"core.add_routine": "auto", "core.delete_routine": "auto"}}}},
            clock=lambda: self.now[0])
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def say(self, text, sender="alice", conversation="room"):
        await self.chat.say(text, sender=sender, conversation=conversation, name=sender.title())
        await settle(self.core)
        return self.chat.texts(conversation)[-1]

    async def fire(self, minutes):
        self.now[0] += minutes * 60
        await self.core.gateway.tick(self.now[0])
        await settle(self.core)

    async def test_a_routine_posts_its_text_as_the_creator_into_its_conversation(self):
        added = await self.say("$routine every 1h echo still here")
        self.assertRegex(added, r"Routine r\w+ added: every 1h")
        await self.say("$routine every 30m $where")
        listing = await self.say("$routine")
        self.assertIn("every 1h · next", listing)
        self.assertIn("· echo still here", listing)
        await self.fire(61)
        texts = self.chat.texts("room")
        self.assertIn("still here", texts)
        self.assertIn("here: chat:room", texts)
        run = next(run for run in self.core.tank.runs(("done",)) if run.origin == "routine")
        self.assertEqual((run.address, run.trigger["sender"]["id"]), ("chat:room", "alice"))

    async def test_members_have_limits_and_manage_only_their_own(self):
        self.assertIn("at most every 30 minutes", await self.say("$routine every 10m too often"))
        for index in range(5):
            await self.say(f"$routine daily 0{index}:00 echo {index}")
        self.assertIn("already have 5 routines", await self.say("$routine daily 07:00 echo six"))
        theirs = self.core.gateway.conversation_routines()[0]["id"]
        self.assertIn("only change routines you created", await self.say(f"$routine delete {theirs}", sender="bob"))
        self.assertIn("only change routines you created",
                      await self.say(f"$routine pause {theirs}", conversation="elsewhere"))
        self.assertEqual(await self.say(f"$routine pause {theirs}"), f"Routine {theirs} paused.")
        self.assertIn("⏸ paused", await self.say("$routine"))
        self.assertEqual(await self.say(f"$routine resume {theirs}"), f"Routine {theirs} resumed.")
        self.assertEqual(await self.say("$routine", conversation="elsewhere"),
                         "No routines in this conversation. Add one with `$routine every 1h <message or $command>`.")

    async def test_admins_see_and_manage_every_conversation_and_add_elsewhere(self):
        await self.say("$routine daily 08:00 echo morning", sender="bob", conversation="other")
        self.core.manager.admins.set("chat:alice", {"added_by": "chat:boss"})
        listing = await self.say("$routine")
        self.assertIn("All routines", listing)
        self.assertIn("chat:other · Bob", listing)
        routine = self.core.gateway.conversation_routines()[0]["id"]
        self.assertEqual(await self.say(f"$routine delete {routine}"), f"Routine {routine} deleted.")
        self.assertIn("in chat:third", await self.say("$routine every 5m --chat chat:third echo ping"))
        await self.say("$admin off")
        self.assertIn("No routines in this conversation", await self.say("$routine"))

    async def test_one_offs_are_removed_after_firing_and_locks_and_closed_chats_skip(self):
        await self.say("$routine at " + dt.datetime.fromtimestamp(self.now[0] + 600).astimezone().strftime("%H:%M")
                       + " echo once")
        await self.say("$routine every 1h echo hourly", sender="bob", conversation="other")
        await self.fire(61)
        self.assertIn("once", self.chat.texts("room"))
        self.assertEqual(self.chat.texts("other").count("hourly"), 1)
        self.assertEqual([item["text"] for item in self.core.gateway.conversation_routines()], ["echo hourly"])
        self.core.manager.settings.set("lock", True)
        await self.fire(61)
        self.core.manager.settings.set("lock", False)
        self.core.gateway.admin().set_rule("chat:other", {"enabled": False})
        await self.fire(61)
        self.assertEqual(self.chat.texts("other").count("hourly"), 1)
        self.assertEqual(len(self.core.gateway.conversation_routines()), 1)

    async def test_models_create_routines_through_the_core_capability(self):
        reply = await self.say('tool core.add_routine {"schedule": "daily 09:00", "text": "what is on today?"}')
        self.assertIn("success: Routine r", reply)
        listing = await self.say('tool core.list_routines {}')
        self.assertIn("daily 09:00", listing)
        self.assertIn("Only admins", await self.say(
            'tool core.add_routine {"schedule": "daily 09:00", "text": "x", "chat": "chat:elsewhere"}'))

    async def test_a_routine_a_model_set_up_does_not_speak_for_its_creator(self):
        for text in ("$remember from the model", "$admin lock", "$routine every 1h $remember laundered"):
            await self.say(f'tool core.add_routine {{"schedule": "every 1h", "text": "{text}"}}', sender="boss")
        await self.say("$routine every 1h $remember typed", sender="boss")
        await self.fire(61)
        texts = self.chat.texts("room")
        self.assertIn("Saved note #1.", texts)  # the typed one, as the creator's own approval
        self.assertEqual([item["arguments"] for item in self.core.tank.approvals("pending")],
                         [{"text": "from the model"}])
        self.assertIn("A routine set up through a tool cannot use $admin; type `$routine` yourself for that.", texts)
        self.assertFalse(self.core.manager.settings.get("lock"))
        laundered = next(item for item in self.core.gateway.conversation_routines() if "laundered" in item["text"])
        self.assertEqual(laundered["via"], "tool")


if __name__ == "__main__":
    unittest.main()

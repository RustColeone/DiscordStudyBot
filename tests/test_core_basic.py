"""The basic module: the time, and notes kept per conversation."""

import tempfile
import unittest
from pathlib import Path

from tests.core_support import iface, settle, start_core


class BasicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = await start_core(Path(self._tmp.name),
                                     {"permissions": {"*": {"rules": {"basic.remember": "auto"}}}})
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def say(self, text, conversation="room", sender="alice"):
        await self.chat.say(text, conversation=conversation, sender=sender)
        await settle(self.core)
        return self.chat.texts(conversation)[-1]

    async def test_notes_stay_in_the_conversation_they_were_saved_in(self):
        self.assertEqual(await self.say("$remember buy milk"), "Saved note #1.")
        self.assertEqual(await self.say("$notes"), "1. buy milk")
        self.assertEqual(await self.say("$notes", conversation="elsewhere", sender="mallory"), "No notes here yet.")

    async def test_notes_from_before_are_shown_to_the_creator_only(self):
        self.core.tank.store("extension:basic").set("notes", [{"text": "old shared note", "at": 0, "by": "x"}])
        self.assertNotIn("old shared note", await self.say("$notes", sender="mallory"))
        self.assertIn("old shared note", await self.say("$notes", conversation="mine", sender="boss"))

    async def test_a_malformed_time_zone_is_explained(self):
        self.assertIn("Unknown IANA timezone", await self.say("$now ../etc"))

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from features.feature_admin import AdminFeature
from features.feature_help import HelpFeature


class AdminFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = SimpleNamespace(waiting=2, max_waiting=5, creator_waiting=1, active=True)
        self.platform = SimpleNamespace(command_queue=self.queue, creator_only_mode=False)
        self.app = SimpleNamespace(platform=self.platform, config={"CREATOR_PROMPT": "private"})
        self.admin = AdminFeature()

    def message(self, content, is_creator):
        return SimpleNamespace(content=content, metadata={"is_creator": is_creator})

    async def test_rejects_non_creator(self):
        response = await self.admin.handle(self.app, self.message("$admin status", False))

        self.assertIn("restricted", response[0].text)

    async def test_creator_can_inspect_queue(self):
        response = await self.admin.handle(self.app, self.message("$admin queue", True))

        self.assertIn("2/5", response[0].text)
        self.assertIn("1` creator", response[0].text)

    async def test_creator_can_toggle_debug_mode(self):
        await self.admin.handle(self.app, self.message("$admin creator-only on", True))
        self.assertTrue(self.platform.creator_only_mode)

        await self.admin.handle(self.app, self.message("$admin creator-only off", True))
        self.assertFalse(self.platform.creator_only_mode)

    async def test_creator_can_clear_waiting_queue(self):
        channel = SimpleNamespace(send=AsyncMock())
        removed = [SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)]
        self.platform.command_queue.clear = Mock(return_value=removed)

        response = await self.admin.handle(self.app, self.message("$admin queue clear", True))

        self.assertIn("Cleared 2", response[0].text)
        self.assertEqual(channel.send.await_count, 2)


class SecretHelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_secret_help_is_creator_only(self):
        feature = HelpFeature()
        creator = SimpleNamespace(content="$help secret", metadata={"is_creator": True})
        other = SimpleNamespace(content="$help secret", metadata={"is_creator": False})

        creator_response = await feature.handle(None, creator)
        other_response = await feature.handle(None, other)

        self.assertIn("$admin creator-only on", creator_response[0].text)
        self.assertNotIn("$admin", other_response[0].text)


if __name__ == "__main__":
    unittest.main()
from types import SimpleNamespace
import unittest

from services.command_queue import CommandQueue, QueuedCommand


def command(name, is_creator=False):
    return QueuedCommand(SimpleNamespace(content=name), SimpleNamespace(), is_creator)


class CommandQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_users_are_fifo(self):
        queue = CommandQueue()
        queue.submit(command("first"))
        queue.submit(command("second"))

        self.assertEqual((await queue.get()).message.content, "first")
        self.assertEqual((await queue.get()).message.content, "second")

    async def test_creator_cuts_ahead_of_waiting_users(self):
        queue = CommandQueue()
        queue.submit(command("user-one"))
        queue.submit(command("creator-one", is_creator=True))
        queue.submit(command("creator-two", is_creator=True))
        queue.submit(command("user-two"))

        self.assertEqual((await queue.get()).message.content, "creator-one")
        self.assertEqual((await queue.get()).message.content, "creator-two")
        self.assertEqual((await queue.get()).message.content, "user-one")

    def test_sixth_user_command_is_dropped(self):
        queue = CommandQueue(max_waiting=5)
        for index in range(5):
            accepted, _ = queue.submit(command(f"user-{index}"))
            self.assertTrue(accepted)

        accepted, dropped = queue.submit(command("user-5"))

        self.assertFalse(accepted)
        self.assertIsNone(dropped)
        self.assertEqual(queue.waiting, 5)

    async def test_creator_can_exceed_limit_without_evicting_users(self):
        queue = CommandQueue(max_waiting=5)
        for index in range(5):
            queue.submit(command(f"user-{index}"))

        accepted, dropped = queue.submit(command("creator", is_creator=True))

        self.assertTrue(accepted)
        self.assertIsNone(dropped)
        self.assertEqual(queue.waiting, 6)
        self.assertEqual((await queue.get()).message.content, "creator")
        self.assertEqual((await queue.get()).message.content, "user-0")

    def test_clear_removes_all_waiting_commands(self):
        queue = CommandQueue()
        queue.submit(command("user"))
        queue.submit(command("creator", is_creator=True))

        removed = queue.clear()

        self.assertEqual({item.message.content for item in removed}, {"user", "creator"})
        self.assertEqual(queue.waiting, 0)


if __name__ == "__main__":
    unittest.main()
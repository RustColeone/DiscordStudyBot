import unittest
from unittest.mock import AsyncMock

from services.status_log import StatusLog


class Message:
    def __init__(self, text):
        self.text = text


class StatusLogTests(unittest.IsolatedAsyncioTestCase):
    def make(self, limit=1900):
        self.now = 0.0
        self.sent = []

        async def send(destination, text):
            message = Message(text)
            self.sent.append((destination, message))
            return message

        async def edit(message, text):
            if getattr(message, "deleted", False):
                raise RuntimeError("Unknown Message")
            message.text = text

        return StatusLog(send, edit, clock=lambda: self.now, limit=limit)

    async def test_lines_are_appended_to_one_code_block_per_destination(self):
        log = self.make()
        await log.append("dm", "first")
        await log.append("dm", "second\nwith detail")
        await log.append("other", "elsewhere")
        self.assertEqual([destination for destination, _ in self.sent], ["dm", "other"])
        text = self.sent[0][1].text
        self.assertTrue(text.startswith("```\n") and text.endswith("\n```"))
        self.assertIn("first", text)
        self.assertIn("second\n  with detail", text)

    async def test_a_block_is_released_after_an_hour(self):
        log = self.make()
        await log.append("dm", "early")
        self.now = 3599
        await log.append("dm", "still in the first block")
        self.now = 3600
        await log.append("dm", "new block")
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn("new block", self.sent[0][1].text)
        self.assertEqual(log._blocks["dm"]["lines"][0].split(" ", 1)[1], "new block")

    async def test_a_full_block_or_a_deleted_message_starts_a_new_one(self):
        log = self.make(limit=120)
        await log.append("dm", "x" * 50)
        await log.append("dm", "y" * 50)
        self.assertEqual(len(self.sent), 2)
        self.sent[-1][1].deleted = True
        await log.append("dm", "after deletion")
        self.assertEqual(len(self.sent), 3)

    async def test_code_fences_in_text_cannot_close_the_block(self):
        log = self.make()
        await log.append("dm", "look ``` here")
        self.assertEqual(self.sent[0][1].text.count("```"), 2)

    async def test_a_destination_that_cannot_receive_keeps_no_block(self):
        log = StatusLog(AsyncMock(return_value=None), AsyncMock())
        self.assertIsNone(await log.append("dm", "nothing"))
        self.assertEqual(log._blocks, {})


if __name__ == "__main__":
    unittest.main()

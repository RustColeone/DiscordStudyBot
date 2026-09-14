from types import SimpleNamespace
import unittest

from features.feature_help import HelpFeature


class HelpFeatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.feature = HelpFeature()

    @staticmethod
    def message(content):
        return SimpleNamespace(content=content, metadata={"is_creator": False})

    async def test_overview_is_a_short_fenced_command_index(self):
        responses = await self.feature.handle(None, self.message("$help"))

        text = responses[0].text
        self.assertLessEqual(len(text), 2000)
        self.assertTrue(text.startswith("```md\n"))
        self.assertTrue(text.endswith("\n```"))
        self.assertIn("$agent <request>", text)
        self.assertIn("$help <command> [page]", text)
        self.assertNotIn("DeepSeek speaks first", text)

    async def test_long_chat_help_is_paged_with_complete_code_fences(self):
        first = (await self.feature.handle(None, self.message("$help chat")))[0].text
        second = (await self.feature.handle(None, self.message("$help chat 2")))[0].text

        for text in (first, second):
            self.assertLessEqual(len(text), 2000)
            self.assertTrue(text.startswith("```md\n"))
            self.assertTrue(text.endswith("\n```"))
            self.assertEqual(text.count("```"), 2)
        self.assertIn("Page 1/2", first)
        self.assertIn("Next: $help chat 2", first)
        self.assertIn("Page 2/2", second)
        self.assertIn("Previous: $help chat 1", second)
        self.assertNotEqual(first, second)

    async def test_page_errors_are_fenced(self):
        response = await self.feature.handle(None, self.message("$help chat 99"))

        self.assertEqual(
            response[0].text,
            "```md\nPage 99 not found for chat. Available pages: 1-2\n```",
        )

    async def test_every_advertised_topic_returns_one_safe_page(self):
        help_content = self.feature.help_path.read_text(encoding="utf-8")

        for topic in self.feature._get_topics(help_content):
            with self.subTest(topic=topic):
                response = await self.feature.handle(None, self.message(f"$help {topic}"))
                text = response[0].text
                self.assertLessEqual(len(text), 2000)
                self.assertTrue(text.startswith("```md\n"))
                self.assertTrue(text.endswith("\n```"))
                self.assertEqual(text.count("```"), 2)


if __name__ == "__main__":
    unittest.main()
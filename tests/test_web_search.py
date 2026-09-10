from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.bot_types import IncomingMessage
from features.feature_search import SearchFeature
from providers import google_query


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def message(self):
        return IncomingMessage(
            content="$google -s current weather",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )

    def test_keyless_search_normalizes_duckduckgo_results(self):
        result = {"title": "Weather", "href": "https://example.com", "body": "Forecast"}
        with patch.object(google_query, "searxng_url", ""), \
                patch("providers.google_query.DDGS") as ddgs:
            ddgs.return_value.text.return_value = [result]
            results = google_query.queryWeb("weather")

        self.assertEqual(results, [{
            "title": "Weather",
            "url": "https://example.com",
            "snippet": "Forecast",
        }])

    async def test_search_failure_falls_back_to_model_knowledge(self):
        unavailable = google_query.WebSearchUnavailable("backends unavailable")
        with patch("features.feature_search.googleQuery.queryWeb", side_effect=unavailable), \
                patch("features.feature_search.unified_chat.query_chat", return_value="Best available answer") as query:
            responses = await SearchFeature().handle(SimpleNamespace(config={}), self.message())

        self.assertEqual(responses[0].text, "Best available answer")
        self.assertFalse(responses[0].metadata["live_search"])
        self.assertEqual(query.call_args.args[0], "current weather")
        self.assertIn("Live web search was attempted", query.call_args.args[4])
        self.assertTrue(query.call_args.args[5])

    async def test_successful_search_synthesizes_answer_with_sources(self):
        results = [{"title": "Weather", "url": "https://example.com", "snippet": "Forecast"}]
        with patch("features.feature_search.googleQuery.queryWeb", return_value=results), \
                patch("features.feature_search.unified_chat.query_chat", return_value="It will rain [1].") as query:
            responses = await SearchFeature().handle(SimpleNamespace(config={}), self.message())

        self.assertTrue(responses[0].text.startswith("It will rain [1]."))
        self.assertIn("[Weather](https://example.com)", responses[0].text)
        self.assertNotIn("Forecast", responses[0].text)
        self.assertTrue(responses[0].metadata["live_search"])
        self.assertEqual(query.call_args.args[0], "current weather")
        self.assertIn("Synthesize the evidence", query.call_args.args[4])
        self.assertIn("Forecast", query.call_args.args[4])


if __name__ == "__main__":
    unittest.main()
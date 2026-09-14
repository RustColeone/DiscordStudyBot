from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from providers import openrouter_query


class OpenRouterQueryTests(unittest.TestCase):
    @staticmethod
    def response(content="done", finish_reason="stop", annotations=None):
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=content, annotations=annotations or []),
        )])

    def test_completion_sends_unified_max_reasoning(self):
        client = Mock()
        client.chat.completions.create.return_value = self.response()

        with patch.object(openrouter_query, "client", client):
            response = openrouter_query.create_completion(
                [{"role": "user", "content": "Plan this"}],
                "openai/example-model",
                effort="max",
                max_tokens=8000,
            )

        self.assertEqual(openrouter_query.completion_text(response), "done")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "openai/example-model")
        self.assertEqual(request["max_tokens"], 8000)
        self.assertEqual(request["extra_body"]["reasoning"], {
            "effort": "max",
            "exclude": True,
        })

    def test_web_search_uses_plugin_and_returns_unique_citations(self):
        citation = {
            "type": "url_citation",
            "url_citation": {"title": "Example", "url": "https://example.com"},
        }
        client = Mock()
        client.chat.completions.create.return_value = self.response(
            "Grounded answer",
            annotations=[citation, citation],
        )

        with patch.object(openrouter_query, "client", client), \
                patch.object(openrouter_query, "WEB_SEARCH_MODEL", "openrouter/auto"), \
                patch.object(openrouter_query, "WEB_SEARCH_ENGINE", "exa"), \
                patch.object(openrouter_query, "WEB_SEARCH_MAX_RESULTS", 3):
            answer, sources = openrouter_query.query_web("What changed?", "latest changes")

        self.assertEqual(answer, "Grounded answer")
        self.assertEqual(sources, [{
            "title": "Example",
            "url": "https://example.com",
            "provider": "OpenRouter Web",
        }])
        plugins = client.chat.completions.create.call_args.kwargs["extra_body"]["plugins"]
        self.assertEqual(plugins, [{"id": "web", "max_results": 3, "engine": "exa"}])

    def test_blank_content_filter_response_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "content_filter"):
            openrouter_query.completion_text(
                self.response(content="", finish_reason="content_filter")
            )

    def test_agent_model_must_be_explicit(self):
        client = Mock()
        with patch.object(openrouter_query, "client", client):
            with self.assertRaisesRegex(RuntimeError, "model is not configured"):
                openrouter_query.create_completion([], "")


if __name__ == "__main__":
    unittest.main()
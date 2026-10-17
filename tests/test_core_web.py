"""Tests for the web extension: search fallbacks and the public page reader."""

import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import ROOT, extension_module, iface, make_config, settle, start_core


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", encoding="utf-8"):
        self.status_code, self.headers, self.body, self.encoding = status, headers or {}, body, encoding
        self.is_redirect = status in (301, 302, 303, 307)
        self.is_permanent_redirect = status in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.pages[url]


def public_dns(host, port, *args, **kwargs):
    address = {"example.com": "93.184.215.14", "evil.example": "10.0.0.5"}[host]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


class WebCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        extensions = [*make_config(tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "web")]
        self.core = await start_core(tmp, {"extensions": extensions,
                                           "extension_config": {"web": {"searxng_url": "http://searx.local"}}})
        self.module = extension_module(self.core, "web")
        self.pages = self.module.pages if hasattr(self.module, "pages") else None
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()


class SearchTests(WebCase):
    async def test_grounded_answer_is_preferred_and_link_backends_are_fallbacks(self):
        searcher_class = self.module.Searcher
        grounded = {"value": ("Paris is the capital.", [{"title": "Wiki", "url": "https://w.org/paris"}])}

        def fake_grounded(searcher, query):
            if isinstance(grounded["value"], Exception):
                raise grounded["value"]
            return grounded["value"]
        searx = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"results": [
            {"title": "Result", "url": "https://r.org", "content": "snippet"}]})
        with patch.object(searcher_class, "gemini", lambda searcher: object()), \
                patch.object(searcher_class, "grounded", fake_grounded), \
                patch.object(self.module.requests, "get", return_value=searx):
            await self.chat.say("$search capital of france")
            await settle(self.core)
            grounded["value"] = RuntimeError("quota")
            await self.chat.say("$search capital of france", conversation="fallback")
            await settle(self.core)
        self.assertEqual(self.chat.texts("room")[0], "Paris is the capital.\n- Wiki: https://w.org/paris")
        self.assertEqual(self.chat.texts("fallback")[0], "- Result: https://r.org\n  snippet")

    async def test_search_reports_every_backend_failure(self):
        class BrokenDDGS:
            def __init__(self, timeout):
                pass

            def text(self, query, max_results):
                raise RuntimeError("ratelimited")
        with patch.object(self.module.Searcher, "gemini", lambda searcher: None), \
                patch.object(self.module.requests, "get", side_effect=OSError("refused")), \
                patch("ddgs.DDGS", BrokenDDGS):
            await self.chat.say("$search anything")
            await settle(self.core)
        text = self.chat.texts("room")[0]
        self.assertIn("SearXNG: refused", text)
        self.assertIn("DuckDuckGo: ratelimited", text)


class PageTests(WebCase):
    def fetch(self, url, pages):
        pages_module = __import__(self.module.__name__.rsplit(".", 1)[0] + ".pages", fromlist=["fetch"])
        with patch.object(pages_module.socket, "getaddrinfo", public_dns):
            return pages_module, pages_module.fetch(url, FakeSession(pages))

    def test_html_is_reduced_to_text_title_and_links(self):
        html = (b"<html><head><title>Hello</title><style>x{}</style></head><body><p>First line</p>"
                b"<script>evil()</script><a href='/next'>Next page</a><a href='mailto:a@b'>mail</a></body></html>")
        _, page = self.fetch("https://example.com/a", {"https://example.com/a": FakeResponse(
            headers={"Content-Type": "text/html; charset=utf-8"}, body=html)})
        self.assertEqual(page["title"], "Hello")
        self.assertIn("First line", page["text"])
        self.assertNotIn("evil", page["text"])
        self.assertEqual(page["links"], [{"title": "Next page", "url": "https://example.com/next"}])

    def test_private_targets_redirects_into_private_networks_and_odd_ports_are_refused(self):
        pages_module, _ = self.fetch("https://example.com/", {"https://example.com/": FakeResponse(
            headers={"Content-Type": "text/plain"}, body=b"ok")})
        for url in ("http://127.0.0.1/", "http://localhost/x", "http://169.254.169.254/latest",
                    "https://evil.example/", "https://example.com:8443/", "ftp://example.com/",
                    "https://user:pw@example.com/"):
            with self.subTest(url=url), self.assertRaises(pages_module.PageUnavailable):
                self.fetch(url, {})
        with self.assertRaises(pages_module.PageUnavailable):
            self.fetch("https://example.com/r", {"https://example.com/r": FakeResponse(
                302, {"Location": "http://10.0.0.1/admin"})})

    def test_oversized_and_unsupported_pages_are_refused(self):
        pages_module, _ = self.fetch("https://example.com/", {"https://example.com/": FakeResponse(
            headers={"Content-Type": "text/plain"}, body=b"ok")})
        with self.assertRaisesRegex(pages_module.PageUnavailable, "too large"):
            self.fetch("https://example.com/big", {"https://example.com/big": FakeResponse(
                headers={"Content-Type": "text/plain"}, body=b"x" * (pages_module.MAX_PAGE_BYTES + 1))})
        with self.assertRaisesRegex(pages_module.PageUnavailable, "Unsupported"):
            self.fetch("https://example.com/i", {"https://example.com/i": FakeResponse(
                headers={"Content-Type": "image/png"}, body=b"png")})

    async def test_read_command_returns_page_text_and_explains_refusals(self):
        pages_module = __import__(self.module.__name__.rsplit(".", 1)[0] + ".pages", fromlist=["fetch"])
        session = FakeSession({"https://example.com/": FakeResponse(
            headers={"Content-Type": "text/html"}, body=b"<title>T</title><p>Body text</p>")})
        original = pages_module.fetch
        with patch.object(pages_module.socket, "getaddrinfo", public_dns), \
                patch.object(self.module, "fetch", lambda url: original(url, session)):
            await self.chat.say("$read <https://example.com/>")
            await self.chat.say("$read http://127.0.0.1/", conversation="local")
            await settle(self.core)
        self.assertEqual(self.chat.texts("room", "text"), ["# T\nhttps://example.com/\n\nT\nBody text"])
        self.assertIn("Local or non-public", self.chat.texts("local", "text")[0])


if __name__ == "__main__":
    unittest.main()

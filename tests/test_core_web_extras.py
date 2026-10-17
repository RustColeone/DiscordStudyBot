"""Tests for the web extension's image search, Google Maps grounding and Wolfram|Alpha capabilities."""

import os
import socket
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from PIL import Image

from tests.core_support import ROOT, extension_module, iface, make_config, settle, start_core


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", payload=None):
        self.status_code, self.headers, self.body, self.payload = status, headers or {}, body, payload
        self.is_redirect = status in (301, 302, 303, 307)
        self.is_permanent_redirect = status in (301, 308)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]

    def json(self):
        return self.payload

    def close(self):
        pass


class FakeSession:
    def __init__(self, pages):
        self.pages, self.requested = pages, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.pages[url]


def public_dns(host, port, *args, **kwargs):
    address = {"example.com": "93.184.215.14", "img.example": "93.184.215.15", "evil.example": "10.0.0.5"}[host]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


def png(size=(40, 30), noise=False):
    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)) if noise else Image.new("RGB", size, "red")
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def fake_ddgs(results, calls=None):
    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def images(self, query, **kwargs):
            if calls is not None:
                calls.append((query, kwargs))
            if isinstance(results, Exception):
                raise results
            return results
    return FakeDDGS


class WebExtrasCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "web")]
        self.core = await start_core(self.tmp, {"extensions": extensions})
        self.module = extension_module(self.core, "web")
        package = self.module.__name__.rsplit(".", 1)[0]
        self.images = __import__(package + ".images", fromlist=["locate_image"])
        self.pages = __import__(package + ".pages", fromlist=["fetch"])
        self.wolfram = __import__(package + ".wolfram", fromlist=["query"])
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def call(self, capability, arguments, conversation="room"):
        import json
        await self.chat.say(f"$call {capability} {json.dumps(arguments)}", conversation=conversation, sender="boss")
        await settle(self.core)

    def replies(self, conversation="room"):
        return [reply for reply in self.chat.delivered if reply.address.conversation == conversation]


class ImageSearchTests(WebExtrasCase):
    async def test_first_downloadable_public_image_is_saved_in_the_workspace_and_sent(self):
        calls = []
        results = [{"image": "data:image/png;base64,xx"},
                   {"image": "http://127.0.0.1/secret.png"},
                   {"image": "https://example.com/redirect.png"},
                   {"image": "https://example.com/page.html"},
                   {"image": "https://img.example/cat.png", "title": "A cat", "url": "https://example.com/cats"}]
        session = FakeSession({
            "https://example.com/redirect.png": FakeResponse(302, {"Location": "http://evil.example/x.png"}),
            "https://example.com/page.html": FakeResponse(headers={"Content-Type": "text/html"}, body=b"<html>"),
            "https://img.example/cat.png": FakeResponse(headers={"Content-Type": "image/png"}, body=png()),
        })
        with patch("ddgs.DDGS", fake_ddgs(results, calls)), \
                patch.object(self.pages.socket, "getaddrinfo", public_dns), \
                patch.object(self.images, "requests", session):
            await self.call("web.image_search", {"query": "cat"})
        self.assertEqual(calls, [("cat", {"max_results": 8, "safesearch": "moderate"})])
        self.assertEqual(session.requested, ["https://example.com/redirect.png", "https://example.com/page.html",
                                             "https://img.example/cat.png"])
        texts = [reply.text for reply in self.replies() if reply.kind == "text"]
        self.assertEqual(texts, ["Supplementary image: A cat\nSource: https://example.com/cats"])
        files = [reply.file for reply in self.replies() if reply.kind == "file"]
        self.assertEqual(len(files), 1)
        path = Path(files[0].path)
        self.assertEqual(files[0].mime, "image/png")
        self.assertTrue(path.name.startswith("reference-") and path.suffix == ".png")
        self.assertEqual(path.parent.parent, self.tmp / "data" / "workspaces")
        with Image.open(path) as image:
            self.assertEqual(image.size, (40, 30))

    async def test_failures_are_reported_without_files(self):
        with patch("ddgs.DDGS", fake_ddgs([{"image": "http://localhost/a.png"}])), \
                patch.object(self.pages.socket, "getaddrinfo", public_dns):
            await self.call("web.image_search", {"query": "cat"})
        with patch("ddgs.DDGS", fake_ddgs(RuntimeError("ratelimited"))):
            await self.call("web.image_search", {"query": "dog"}, conversation="other")
        self.assertIn("Image search failed: Local network links are not supported", self.chat.texts("room", "text")[0])
        self.assertIn("Image search failed: ratelimited", self.chat.texts("other", "text")[0])
        self.assertFalse([reply for reply in self.chat.delivered if reply.kind == "file"])

    def test_images_are_downscaled_and_fitted_to_the_upload_limit(self):
        directory = self.tmp / "out"
        path, mime = self.images.save_image(png((3600, 900)), directory, 10_000_000)
        with Image.open(path) as image:
            self.assertEqual((image.size, mime), ((1800, 450), "image/png"))
        noisy = png((400, 400), noise=True)
        path, mime = self.images.save_image(noisy, directory, 300_000)
        self.assertEqual((path.suffix, mime), (".jpg", "image/jpeg"))
        with self.assertRaisesRegex(self.images.ImageUnavailable, "too large"):
            self.images.save_image(noisy, directory, 1000)
        with self.assertRaisesRegex(self.images.ImageUnavailable, "not a supported image"):
            self.images.save_image(b"not an image", directory, 1000)
        self.assertEqual(sorted(item.suffix for item in directory.iterdir()), [".jpg", ".png"])

    def test_downloads_are_size_limited(self):
        session = FakeSession({"https://img.example/big.png": FakeResponse(body=b"x" * 101)})
        with patch.object(self.pages.socket, "getaddrinfo", public_dns), \
                self.assertRaisesRegex(self.images.ImageUnavailable, "too large"):
            self.images.download("https://img.example/big.png", 100, session)
        session = FakeSession({"https://img.example/big.png": FakeResponse(headers={"Content-Length": "500"})})
        with patch.object(self.pages.socket, "getaddrinfo", public_dns), \
                self.assertRaisesRegex(self.images.ImageUnavailable, "too large"):
            self.images.download("https://img.example/big.png", 100, session)


class FakeGemini:
    def __init__(self, text, annotations=(), error=None):
        self.calls, self.text, self.annotations, self.error = [], text, annotations, error
        self.interactions = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        block = SimpleNamespace(annotations=list(self.annotations))
        return SimpleNamespace(output_text=self.text,
                               steps=[SimpleNamespace(type="model_output", content=[block])])


class MapsTests(WebExtrasCase):
    async def test_maps_uses_google_maps_grounding_and_lists_places(self):
        gemini = FakeGemini("Cafe Luna is open until 22:00.", [
            SimpleNamespace(url="https://maps.google.com/?cid=1", title=None, name="Cafe Luna"),
            SimpleNamespace(url="https://maps.google.com/?cid=1", title="dup", name=None),
            SimpleNamespace(url="https://maps.google.com/?cid=2", title=None, name=None)])
        with patch.object(self.module.Searcher, "gemini", lambda searcher: gemini):
            await self.call("web.maps", {"query": "cafes open late in Seattle"})
        self.assertEqual(gemini.calls[0]["tools"], [{"type": "google_maps"}])
        self.assertEqual(gemini.calls[0]["input"], "cafes open late in Seattle")
        self.assertEqual(self.replies()[0].text, "Cafe Luna is open until 22:00.\n"
                                                 "- Cafe Luna: https://maps.google.com/?cid=1\n"
                                                 "- Google Maps: https://maps.google.com/?cid=2")

    async def test_maps_failures_are_explained(self):
        with patch.object(self.module.Searcher, "gemini", lambda searcher: None):
            await self.call("web.maps", {"query": "museums in Paris"})
        with patch.object(self.module.Searcher, "gemini", lambda searcher: FakeGemini("", error=OSError("quota"))):
            await self.call("web.maps", {"query": "museums in Paris"}, conversation="broken")
        with patch.object(self.module.Searcher, "gemini", lambda searcher: FakeGemini("")):
            await self.call("web.maps", {"query": "museums in Paris"}, conversation="empty")
        self.assertIn("not configured", self.replies()[0].text)
        self.assertIn("unavailable right now: quota", self.replies("broken")[0].text)
        self.assertIn("no grounded evidence", self.replies("empty")[0].text)


class WolframTests(WebExtrasCase):
    PODS = {"queryresult": {"success": True, "error": False, "pods": [
        {"title": "Indefinite integral", "subpods": [{"plaintext": "integral x^2 dx = x^3/3 + constant"}]},
        {"title": "Plot", "subpods": [{"plaintext": ""}]}]}}

    async def test_wolfram_command_accepts_legacy_flags_and_formats_pods(self):
        requested = []

        def fake_get(url, params, timeout):
            requested.append((url, params, timeout))
            return FakeResponse(payload=self.PODS)
        with patch.dict(os.environ, {"WOLFRAM_APPID": "APP-123"}), \
                patch.object(self.wolfram.requests, "get", fake_get):
            await self.chat.say('$wolfram -q "integrate x^2"')
            await self.chat.say("$wolfram 2+2", conversation="plain")
            await settle(self.core)
        self.assertEqual(requested[0][0], "https://api.wolframalpha.com/v2/query")
        self.assertEqual(requested[0][1], {"appid": "APP-123", "input": "integrate x^2", "format": "plaintext",
                                           "output": "json"})
        self.assertEqual(requested[1][1]["input"], "2+2")
        self.assertEqual(self.chat.texts("room")[0],
                         "## Indefinite integral\nintegral x^2 dx = x^3/3 + constant\n\n## Plot")

    async def test_wolfram_failures_are_explained_and_never_leak_the_appid(self):
        def leaking(url, params, timeout):
            raise ConnectionError(f"Max retries exceeded with url: /v2/query?appid={params['appid']}")
        with patch.dict(os.environ, {"WOLFRAM_APPID": ""}):
            await self.chat.say("$wolfram 2+2")
            await settle(self.core)
        with patch.dict(os.environ, {"WOLFRAM_APPID": "APP-123"}):
            with patch.object(self.wolfram.requests, "get", leaking):
                await self.chat.say("$wolfram 2+2", conversation="down")
                await settle(self.core)
            with patch.object(self.wolfram.requests, "get", lambda url, params, timeout: FakeResponse(
                    payload={"queryresult": {"success": False, "error": False}})):
                await self.chat.say("$wolfram blorp", conversation="nonsense")
                await settle(self.core)
            with patch.object(self.wolfram.requests, "get", lambda url, params, timeout: FakeResponse(
                    payload={"queryresult": {"error": {"code": "1", "msg": "Invalid appid APP-123"}}})):
                await self.chat.say("$wolfram 2+2", conversation="badkey")
                await settle(self.core)
        self.assertIn("not configured", self.chat.texts("room")[0])
        self.assertIn("unavailable right now", self.chat.texts("down")[0])
        self.assertIn("could not interpret", self.chat.texts("nonsense")[0])
        self.assertEqual(self.chat.texts("badkey")[0],
                         "Wolfram|Alpha is not configured correctly: Invalid appid ***")
        self.assertFalse(any("APP-123" in (reply.text or "") for reply in self.chat.delivered))

    def test_rejected_appids_are_not_retryable_failures(self):
        class Unauthorized(FakeResponse):
            def raise_for_status(self):
                raise requests.HTTPError(f"{self.status_code} for url ...appid=APP-123", response=self)
        with patch.object(self.wolfram.requests, "get", lambda url, params, timeout: Unauthorized(401)), \
                self.assertRaisesRegex(self.wolfram.WolframRejected, "^HTTP 401: the AppID was rejected$"):
            self.wolfram.query("APP-123", "2+2")
        with patch.object(self.wolfram.requests, "get", lambda url, params, timeout: Unauthorized(500)), \
                self.assertRaises(self.wolfram.WolframUnavailable) as caught:
            self.wolfram.query("APP-123", "2+2")
        self.assertNotIsInstance(caught.exception, self.wolfram.WolframRejected)

    def test_command_input_parsing(self):
        parse = self.wolfram.command_input
        self.assertEqual(parse('--query "what is pi"'), "what is pi")
        self.assertEqual(parse("-q integrate x^2"), "integrate x^2")
        self.assertEqual(parse("-5 + 3"), "-5 + 3")
        self.assertEqual(parse('-q "unbalanced'), '"unbalanced')


if __name__ == "__main__":
    unittest.main()

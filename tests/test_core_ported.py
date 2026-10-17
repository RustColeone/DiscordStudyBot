"""Tests for the ported media, images and system extensions inside a real core."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import ROOT, extension_module, iface, settle, start_core

def config(tmp):
    from tests.core_support import make_config
    base = make_config(Path(tmp)).raw["modules"]["paths"]
    return {"extensions": [*base, str(ROOT / "extensions" / "media"), str(ROOT / "extensions" / "images"),
                           str(ROOT / "extensions" / "system")],
            "extension_config": {"fake": {"interfaces": {"chat": {"quotes": True, "max_upload_mb": 1},
                                                         "owner": {"progress": True},
                                                         "nofiles": {"files": False}}}}}


class PortedCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = await start_core(self.tmp, config(self.tmp))

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()


class SystemTests(PortedCase):
    async def test_status_command_reports_host_resources(self):
        await iface(self.core).say("$status")
        await settle(self.core)
        text = iface(self.core).texts("room", "text")[0]
        for label in ("CPU:", "Memory:", "Storage:", "Uptime:"):
            self.assertIn(label, text)


class ImageTests(PortedCase):
    async def test_draw_delivers_generated_images_and_reports_provider_problems(self):
        module = extension_module(self.core, "images")
        calls = []

        def fake_generate(generator, provider, prompt, references, directory):
            calls.append((provider, prompt, [ref.name for ref in references]))
            if provider == "openai":
                raise LookupError("openai image generation is not configured (no API key)")
            path = directory / "gemini-1.png"
            path.write_bytes(b"png")
            return [path], "a cat"
        with patch.object(module.ImageGenerator, "generate", fake_generate):
            await iface(self.core).say("$draw a cat on a moon")
            await iface(self.core).say('tool images.generate {"prompt": "a dog", "provider": "openai"}', conversation="o")
            await iface(self.core).say('tool images.generate {"prompt": "redo", "use_attached_images": true}',
                                       conversation="ref")
            await settle(self.core)
        chat = iface(self.core)
        self.assertEqual([r.file.name for r in chat.delivered if r.kind == "file"], ["gemini-1.png"])
        self.assertIn("Generated 1 image(s) with gemini", chat.texts("room", "text")[0])
        self.assertIn("not configured", chat.texts("o", "text")[0])
        self.assertIn("No attached image", chat.texts("ref", "text")[0])
        self.assertEqual(calls[0][:2], ("gemini", "a cat on a moon"))

    async def test_attached_images_are_used_as_references_when_requested(self):
        from contracts import Address, FileRef, Request, Sender
        module = extension_module(self.core, "images")
        image = self.tmp / "photo.png"
        image.write_bytes(b"png")
        seen = []

        def fake_generate(generator, provider, prompt, references, directory):
            seen.extend(ref.path for ref in references)
            path = directory / "out.png"
            path.write_bytes(b"png")
            return [path], ""
        chat = iface(self.core)
        with patch.object(module.ImageGenerator, "generate", fake_generate):
            await chat.port.submit(Request("img-1", Address("chat", "room"), Sender("alice", "Alice"),
                                           'tool images.generate {"prompt": "anime style", "use_attached_images": true}',
                                           time.time(), display=chat.display,
                                           files=(FileRef(str(image), "photo.png", "image/png", 3),)))
            await settle(self.core)
        self.assertEqual(seen, [str(image)])


class FakeYoutubeDL:
    fixture: Path = None
    formats: list = []
    duration = 2.0
    requested: list = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *error):
        return False

    def extract_info(self, url, download):
        assert download is False
        return {"formats": self.formats, "ext": "mp4", "duration": self.duration, "title": "Fixture: clip/1",
                "extractor_key": "Test"}

    def process_ie_result(self, info, download):
        FakeYoutubeDL.requested.append(self.options["format"])
        shutil.copy(self.fixture, self.prepare_filename(info))
        return info

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(ext)s", "mp4")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
class MediaTests(PortedCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        fixture = self.tmp / "fixture.mp4"
        await asyncio.to_thread(subprocess.run, [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2", "-f", "lavfi",
            "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(fixture)], check=True)
        FakeYoutubeDL.fixture, FakeYoutubeDL.duration, FakeYoutubeDL.requested = fixture, 2.0, []
        FakeYoutubeDL.formats = [
            {"format_id": "v", "ext": "mp4", "vcodec": "avc1.64000d", "acodec": "none", "width": 320, "height": 180,
             "fps": 30, "filesize": fixture.stat().st_size},
            {"format_id": "a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 64, "filesize": 1}]
        self.patch = patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_full_video_stream_copies_a_fitting_source_and_delivers_it(self):
        await iface(self.core).say("$video https://example.com/v")
        await settle(self.core)
        chat = iface(self.core)
        files = [r for r in chat.delivered if r.kind == "file"]
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].file.name, "Fixture_clip_1.mp4")
        self.assertIn("copied without re-encoding", chat.texts("room", "text")[0])
        self.assertEqual(FakeYoutubeDL.requested, ["v+a"])
        self.assertTrue(any("Inspecting" in text for text in chat.texts("room", "progress")))

    async def test_a_range_is_reencoded_within_the_budget(self):
        await iface(self.core).say('tool media.download_video {"url": "https://example.com/v", "end": "0:01"}')
        await settle(self.core)
        text = iface(self.core).texts("room", "text")[0]
        self.assertTrue(text.startswith("success: Prepared"), text)
        self.assertIn("1s,", text)
        self.assertEqual(FakeYoutubeDL.requested, ["bestvideo+bestaudio/best"])

    async def test_an_unfittable_length_asks_instead_of_downloading(self):
        FakeYoutubeDL.duration = 36000.0
        await iface(self.core).say("$video https://example.com/long")
        await settle(self.core)
        self.assertIn("cannot fit the 1 MB file budget", iface(self.core).texts("room", "text")[0])
        self.assertEqual(FakeYoutubeDL.requested, [])

    async def test_conversations_that_cannot_show_files_cannot_use_it(self):
        await iface(self.core, "nofiles").say("$video https://example.com/v")
        await settle(self.core)
        self.assertIn("needs ['files']", iface(self.core, "nofiles").texts(kind="text")[0])
        self.assertEqual(FakeYoutubeDL.requested, [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the ported $clip flow (media.clip_video and the pending-clip capabilities) inside a real core."""

import asyncio
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import ROOT, iface, make_config, settle, start_core


def config(tmp):
    base = make_config(Path(tmp)).raw["modules"]["paths"]
    return {"extensions": [*base, str(ROOT / "extensions" / "media")],
            "extension_config": {"fake": {"interfaces": {"chat": {"quotes": True, "max_upload_mb": 1},
                                                         "owner": {"progress": True},
                                                         "nofiles": {"files": False},
                                                         "tiny": {"max_upload_mb": 0.001}}}}}


class FakeYoutubeDL:
    fixture: Path = None
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
        if "broken" in url:
            raise OSError("HTTP Error 404")
        return {"formats": [], "ext": "mp4", "duration": self.duration, "title": "Fixture " + url.rsplit("/", 1)[-1],
                "extractor_key": "Test"}

    def process_ie_result(self, info, download):
        FakeYoutubeDL.requested.append(self.options["format"])
        shutil.copy(self.fixture, self.prepare_filename(info))
        return info

    def prepare_filename(self, info):
        return self.options["outtmpl"].replace("%(ext)s", "mp4")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is unavailable")
class ClipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        fixture = self.tmp / "fixture.mp4"
        await asyncio.to_thread(subprocess.run, [
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=2", "-f", "lavfi",
            "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(fixture)], check=True)
        FakeYoutubeDL.fixture, FakeYoutubeDL.duration, FakeYoutubeDL.requested = fixture, 2.0, []
        self.patch = patch.dict(sys.modules, {"yt_dlp": SimpleNamespace(YoutubeDL=FakeYoutubeDL)})
        self.patch.start()
        self.core = await start_core(self.tmp, config(self.tmp))

    async def asyncTearDown(self):
        await self.core.stop()
        self.patch.stop()
        self._tmp.cleanup()

    async def say(self, text, conversation="room", interface="chat"):
        chat = iface(self.core, interface)
        before = len(chat.texts(conversation, "text"))
        await chat.say(text, conversation=conversation)
        await settle(self.core)
        texts = chat.texts(conversation, "text")
        self.assertEqual(len(texts), before + 1, texts)
        return texts[-1]

    def files(self, conversation="room"):
        return [r.file for r in iface(self.core).delivered if r.kind == "file" and r.address.conversation == conversation]

    async def test_preview_adjust_and_confirm_with_skip(self):
        preview = await self.say("$clip -u https://example.com/a -s 0 -e 1 -u https://example.com/b -s 0:01 -e 2")
        self.assertIn("Clip Preview (upload limit: 1MB)", preview)
        self.assertIn("Clip 2:", preview)
        self.assertIn("Duration: 1.0s (0:01 → 0:02)", preview)
        self.assertIn("A) 1080p @ 2500k (30fps) → ~0.32MB ✅", preview)
        self.assertIn("$clip --confirm", preview)
        self.assertEqual(self.files(), [])
        self.assertEqual(FakeYoutubeDL.requested, [])

        updated = await self.say("$clip --clip 1 --resolution 240p --fps 5 --bitrate 200k")
        self.assertIn("Updated Clip 1", updated)
        self.assertIn("resolution → 240p", updated)
        self.assertIn("Settings: 240p @ 200k (5fps)", updated)

        done = await self.say("$clip --confirm --skip 2")
        self.assertIn("Clip 1/1: Fixture a", done)
        self.assertIn("All clips processed", done)
        self.assertEqual([file.name for file in self.files()], ["clip_1.mp4"])
        self.assertEqual(FakeYoutubeDL.requested, ["bestvideo+bestaudio/best"])
        progress = iface(self.core).texts("room", "progress")
        self.assertTrue(any("Processing 1 clip(s)" in text for text in progress), progress)
        self.assertTrue(any("Encoding 1s at 240p, 5 fps" in text for text in progress), progress)
        self.assertIn("No pending clips to process", await self.say("$clip --confirm"))

    async def test_pending_clips_survive_a_restart_and_belong_to_one_conversation(self):
        await self.say("$clip -u https://example.com/a -e 1")
        await self.core.stop()
        self.core = await start_core(self.tmp, config(self.tmp))
        self.assertIn("Fixture a", await self.say("$clip"))
        self.assertIn("No pending clips", await self.say("$clip", conversation="other"))
        listed = await self.say("tool media.list_pending_clips {}")
        self.assertTrue(listed.startswith("success: 📊 Clip Preview"), listed)
        self.assertIn("Cancelled 1 pending clip(s)", await self.say("$clip --cancel"))
        self.assertIn("no pending clips", await self.say("tool media.list_pending_clips {}"))

    async def test_engine_flow_asks_for_a_goal_then_trims_fits_and_confirms(self):
        FakeYoutubeDL.duration = 600.0
        asked = await self.say('tool media.clip_video {"url": "https://example.com/long"}')
        self.assertTrue(asked.startswith("needs_input:"), asked)
        self.assertIn("The full video is about", asked)
        self.assertIn("above this conversation's 1 MB upload limit", asked)

        too_long = await self.say('tool media.adjust_pending_clip {"action": "fit_upload"}')
        self.assertTrue(too_long.startswith("needs_input:"), too_long)
        self.assertIn("600s is too long", too_long)

        trimmed = await self.say('tool media.adjust_pending_clip {"action": "trim", "start": "0", "end": "1"}')
        self.assertTrue(trimmed.startswith("success:"), trimmed)
        self.assertIn("Duration: 1.0s (0:00 → 0:01)", trimmed)
        self.assertIn("media.manage_pending_clips", trimmed)

        tuned = await self.say('tool media.adjust_pending_clip '
                               '{"action": "set_quality", "resolution": "360p", "fps": 5, "bitrate": "150k"}')
        self.assertIn("Selected: 360p @ 150k (5fps), audio 128k", tuned)

        done = await self.say('tool media.manage_pending_clips {"action": "confirm"}')
        self.assertTrue(done.startswith("success:"), done)
        self.assertEqual([file.name for file in self.files()], ["clip_1.mp4"])
        self.assertIn("no pending clips", await self.say("tool media.list_pending_clips {}"))

    async def test_fit_upload_compresses_to_the_conversation_limit(self):
        FakeYoutubeDL.duration = 20.0
        await self.say('tool media.clip_video {"url": "https://example.com/a"}')
        fitted = await self.say('tool media.adjust_pending_clip {"action": "fit_upload"}')
        self.assertTrue(fitted.startswith("success:"), fitted)
        self.assertIn("Selected: 360p @ 240k (24fps), audio 128k", fitted)

    async def test_audio_clips_and_kept_files(self):
        done = await self.say("$clip -u https://example.com/a -e 1 -f mp3 --keep-file --force")
        self.assertIn("Clip 1/1: Fixture a", done)
        self.assertIn("(kept)", done)
        files = self.files()
        self.assertEqual([(file.name, file.mime) for file in files], [("clip_1.mp3", "audio/mpeg")])
        self.assertEqual(FakeYoutubeDL.requested, ["bestaudio/best"])
        kept = list((self.tmp / "data").rglob("media/clips/*.mp3"))
        self.assertEqual(len(kept), 1)
        self.assertIn("No pending clips", await self.say("$clip"))

    async def test_results_above_the_limit_are_deleted_or_kept_on_the_host(self):
        command = "$clip -u https://example.com/a -e 1 -r 240p --fps 5 -b 100k --force"
        deleted = await self.say(command, interface="tiny")
        self.assertIn("above the 0.001MB upload limit. It was deleted", deleted)
        kept = await self.say(command + " --keep-file", interface="tiny")
        self.assertIn("kept on host at", kept)
        self.assertIn("so it was not attached", kept)
        self.assertEqual([r for r in iface(self.core, "tiny").delivered if r.kind == "file"], [])
        self.assertEqual(len(list((self.tmp / "data").rglob("media/clips/*.mp4"))), 1)

    async def test_errors_are_reported_without_downloading(self):
        self.assertIn("Unknown flag: --bogus", await self.say("$clip --bogus"))
        self.assertIn("--resolution must look like 720p", await self.say("$clip -u https://example.com/a -r big"))
        self.assertIn("End time must be after start time", await self.say("$clip -u https://example.com/a -s 5 -e 3"))
        self.assertIn("HTTP Error 404", await self.say("$clip -u https://example.com/broken"))
        self.assertIn("There are no pending clips", await self.say("$clip --clip 2 --fit"))
        self.assertIn("needs ['files']", await self.say("$clip -u https://example.com/a", interface="nofiles"))
        self.assertEqual(FakeYoutubeDL.requested, [])


if __name__ == "__main__":
    unittest.main()

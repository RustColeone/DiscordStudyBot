from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import tempfile

from app.bot_types import IncomingMessage
from features.feature_clip import ClipFeature
from providers import clip_query


class ClipWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clip_query.clear_pending_clips("channel")
        self.addCleanup(clip_query.clear_pending_clips, "channel")
        self.feature = ClipFeature()
        self.message = IncomingMessage(
            content="$clip",
            channel_id="channel",
            author_id="user",
            author_name="User",
            author_display_name="User",
            created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            metadata={"guild_premium_tier": 0},
        )

    async def _create_full_video(self):
        message = IncomingMessage(**{
            **self.message.__dict__,
            "content": "$clip --url https://youtu.be/example",
        })
        with patch(
            "features.feature_clip.clipQuery.get_video_info",
            return_value=("Example", "Youtube", 301),
        ):
            return await self.feature.handle(SimpleNamespace(), message)

    async def test_oversized_full_video_returns_structured_preview(self):
        responses = await self._create_full_video()

        preview = responses[0].metadata["clip_preview"]
        self.assertEqual(preview["limit_mb"], 10)
        self.assertAlmostEqual(preview["clips"][0]["duration"], 301)
        self.assertGreater(preview["clips"][0]["estimated_size_mb"], 10)

    async def test_trim_updates_pending_range_and_estimate(self):
        await self._create_full_video()

        responses = await self.feature.adjust_pending_for_agent(
            self.message,
            {"action": "trim", "start": "1:00", "end": "1:30"},
        )

        clip = clip_query.get_pending_clips("channel")[0]
        self.assertEqual((clip.start, clip.end), (60, 90))
        self.assertEqual(clip.estimated_size_mb, 9.62)
        self.assertEqual(responses[0].metadata["clip_preview"]["clips"][0]["duration"], 30)

    async def test_fit_discord_compresses_full_video_below_limit(self):
        await self._create_full_video()

        await self.feature.adjust_pending_for_agent(
            self.message,
            {"action": "fit_discord"},
        )

        clip = clip_query.get_pending_clips("channel")[0]
        self.assertLessEqual(clip.estimated_size_mb, 9)
        self.assertEqual(clip.fps, 24)

    async def test_fit_discord_recommends_trim_when_bitrate_is_unusable(self):
        clip_query.store_pending_clips("channel", [clip_query.ClipSpec(
            url="https://youtu.be/example",
            start=0,
            end=3600,
            resolution="1080p",
            fps=30,
            bitrate="2500k",
            output_format="mp4",
            estimated_size_mb=1000,
        )])

        responses = await self.feature.adjust_pending_for_agent(
            self.message,
            {"action": "fit_discord"},
        )

        self.assertIn("too long", responses[0].text)
        self.assertEqual(clip_query.get_pending_clips("channel")[0].bitrate, "2500k")

    async def test_keep_full_quality_marks_file_for_host_retention(self):
        await self._create_full_video()

        responses = await self.feature.adjust_pending_for_agent(
            self.message,
            {"action": "keep_full_quality"},
        )

        self.assertTrue(clip_query.get_pending_clips("channel")[0].keep_file)
        self.assertIn("too large for Discord", responses[0].text)

    async def test_direct_download_uses_full_duration_and_upload_fit_without_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            async def render(clip, output):
                seen.append(clip)
                Path(output).write_bytes(b"video fixture")
                return True
            with patch.object(clip_query, "CLIP_OUTPUT_DIR", Path(directory)), \
                    patch.object(clip_query, "get_video_info", return_value=("Example", "BiliBili", 120.8)), \
                    patch.object(clip_query, "create_clip", side_effect=render):
                answers = await self.feature.download_for_agent(self.message, {"url": "https://example.com/video"})
            self.assertEqual((seen[0].start, seen[0].end), (0, 120.8))
            self.assertLessEqual(clip_query.estimate_clip_size(120.8, seen[0].resolution, seen[0].bitrate, seen[0].fps), 10)
            self.assertIsNotNone(answers[0].attachment)
            self.assertIsNone(clip_query.get_pending_clips("channel"))

    async def test_source_failure_never_creates_a_preview_or_claims_success(self):
        with patch.object(clip_query, "get_video_info", side_effect=clip_query.VideoSourceError("No video formats found")):
            answers = await self.feature.download_for_agent(self.message, {"url": "https://example.com/video"})
        self.assertEqual(answers[0].metadata["tool_status"], "failed")
        self.assertEqual(answers[0].metadata["error_code"], "source_unavailable")
        self.assertIn("No video formats found", answers[0].text)
        self.assertIsNone(clip_query.get_pending_clips("channel"))

    async def test_explicit_30fps_keeps_full_duration_and_exposes_encoding_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            async def render(clip, output):
                seen.append(clip)
                Path(output).write_bytes(b"video fixture")
                return True
            with patch.object(clip_query, "CLIP_OUTPUT_DIR", Path(directory)), \
                    patch.object(clip_query, "get_video_info", return_value=("Example", "BiliBili", 120.8)), \
                    patch.object(clip_query, "create_clip", side_effect=render):
                answers = await self.feature.download_for_agent(self.message, {
                    "url": "https://example.com/video", "fps": 30})
            self.assertEqual((seen[0].start, seen[0].end, seen[0].fps), (0, 120.8, 30))
            evidence = answers[0].metadata["video_encoding"]
            self.assertEqual(evidence["duration_seconds"], 120.8)
            self.assertEqual(evidence["fps"], 30)
            self.assertEqual(evidence["video_bitrate"], seen[0].bitrate)
            self.assertLess(evidence["estimated_size_mb"], evidence["upload_limit_mb"])
            self.assertIn("30 fps", answers[0].text)


if __name__ == "__main__":
    unittest.main()

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
import shutil
import subprocess
import tempfile

from app.bot_types import IncomingMessage
from features.feature_clip import ClipFeature
from providers import clip_query


class ClipWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_downloads_reserve_distinct_outputs_before_rendering(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(clip_query, 'CLIP_OUTPUT_DIR', Path(directory)):
            clip = clip_query.ClipSpec('https://example.com/video', 0, 10, video_title='Same title')
            first = Path(clip_query.allocate_clip_output_path(clip, 1))
            second = Path(clip_query.allocate_clip_output_path(clip, 1))
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file() and second.is_file())
            self.assertEqual(first.stat().st_size, 0)

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

    async def test_six_minute_video_fits_user_ten_mb_target_by_adapting_audio(self):
        message = IncomingMessage(**{**self.message.__dict__, 'metadata': {
            'platform':'wechat', 'upload_limit_mb':100, 'upload_limit_source':'bridge_policy'}})
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            async def render(clip, output):
                seen.append(clip)
                Path(output).write_bytes(b'video fixture')
                return True
            with patch.object(clip_query, 'CLIP_OUTPUT_DIR', Path(directory)), \
                    patch.object(clip_query, 'get_video_info', return_value=('Example','BiliBili',385.45)), \
                    patch.object(clip_query, 'create_clip', side_effect=render):
                responses = await self.feature.download_for_agent(message, {
                    'url':'https://example.com/video', 'max_size_mb':10})
            self.assertEqual((seen[0].start,seen[0].end), (0,385.45))
            self.assertEqual(seen[0].audio_bitrate, '64k')
            encoding = responses[0].metadata['video_encoding']
            self.assertEqual(encoding['budget_source'], 'user_target')
            self.assertEqual(encoding['upload_limit_mb'], 10)
            self.assertLessEqual(encoding['estimated_size_mb'], 9)
            self.assertIsNotNone(responses[0].attachment)

    async def test_infeasible_budget_reports_estimate_without_claiming_upload_rejection(self):
        message = IncomingMessage(**{**self.message.__dict__, 'metadata': {
            'platform':'wechat', 'upload_limit_mb':10, 'upload_limit_source':'bridge_policy'}})
        with patch.object(clip_query, 'get_video_info', return_value=('Example','BiliBili',3600)), \
                patch.object(clip_query, 'create_clip') as render:
            responses = await self.feature.download_for_agent(message, {'url':'https://example.com/video'})
        render.assert_not_called()
        self.assertEqual(responses[0].metadata['tool_status'], 'needs_input')
        self.assertEqual(responses[0].metadata['file_budget']['source'], 'bridge_policy')
        self.assertIs(responses[0].metadata['upload_attempted'], False)
        self.assertNotIn('WeChat limit', responses[0].text)

    async def test_explicit_audio_bitrate_is_preserved_and_target_cannot_raise_channel_budget(self):
        message = IncomingMessage(**{**self.message.__dict__, 'metadata': {
            'platform':'wechat', 'upload_limit_mb':10, 'upload_limit_source':'bridge_policy'}})
        with patch.object(clip_query, 'get_video_info', return_value=('Example','BiliBili',385.45)), \
                patch.object(clip_query, 'create_clip') as render:
            responses = await self.feature.download_for_agent(message, {
                'url':'https://example.com/video', 'audio_bitrate':'128k', 'max_size_mb':100})
        render.assert_not_called()
        self.assertEqual(responses[0].metadata['tool_status'], 'needs_input')
        self.assertEqual(responses[0].metadata['file_budget']['max_mb'], 10)

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
            self.assertIsNone(seen[0].copy_budget_mb)
            evidence = answers[0].metadata["video_encoding"]
            self.assertEqual(evidence["duration_seconds"], 120.8)
            self.assertEqual(evidence["fps"], 30)
            self.assertEqual(evidence["video_bitrate"], seen[0].bitrate)
            self.assertLess(evidence["estimated_size_mb"], evidence["upload_limit_mb"])
            self.assertIn("30 fps", answers[0].text)

    async def test_full_video_copies_fitting_h264_source_and_reports_actual_settings(self):
        message = IncomingMessage(**{**self.message.__dict__, 'metadata': {
            'platform':'wechat', 'upload_limit_mb':100, 'upload_limit_source':'bridge_policy'}})
        with tempfile.TemporaryDirectory() as directory:
            seen = []
            async def render(clip, output, progress=None):
                seen.append(clip)
                if clip.copy_budget_mb:
                    clip.stream_copy = clip_query.select_copy_formats(BILIBILI_FORMATS, 385.45, clip.copy_budget_mb)
                Path(output).write_bytes(b'video fixture')
                return True
            with patch.object(clip_query, 'CLIP_OUTPUT_DIR', Path(directory)), \
                    patch.object(clip_query, 'get_video_info', return_value=('Example','BiliBili',385.45)), \
                    patch.object(clip_query, 'create_clip', side_effect=render):
                responses = await self.feature.download_for_agent(message, {'url':'https://example.com/video'})
                ranged = await self.feature.download_for_agent(message, {'url':'https://example.com/video', 'end':'60'})
        self.assertEqual(seen[0].copy_budget_mb, 100)
        self.assertIsNone(seen[1].copy_budget_mb)
        encoding = responses[0].metadata['video_encoding']
        self.assertEqual((encoding['method'], encoding['resolution'], encoding['fps']), ('stream_copy', '1280x548', 30))
        self.assertIn('copied without re-encoding', responses[0].text)
        self.assertEqual(ranged[0].metadata['video_encoding']['method'], 'reencode')


MIB = 1024 * 1024
# Formats reported by yt-dlp for the October 14 Bilibili request (sizes are approximate).
BILIBILI_FORMATS = [
    {'format_id': '30216', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 66, 'filesize_approx': 3.02 * MIB},
    {'format_id': '30280', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 197, 'filesize_approx': 9.07 * MIB},
    {'format_id': '30032', 'ext': 'mp4', 'vcodec': 'avc1.640033', 'acodec': 'none', 'width': 854, 'height': 366,
     'fps': 30, 'tbr': 845, 'filesize_approx': 38.83 * MIB},
    {'format_id': '30064', 'ext': 'mp4', 'vcodec': 'avc1.640033', 'acodec': 'none', 'width': 1280, 'height': 548,
     'fps': 30, 'tbr': 1555, 'filesize_approx': 71.47 * MIB},
    {'format_id': '30080', 'ext': 'mp4', 'vcodec': 'avc1.640033', 'acodec': 'none', 'width': 1920, 'height': 822,
     'fps': 30, 'tbr': 2753, 'filesize_approx': 126.5 * MIB},
    {'format_id': '30077', 'ext': 'mp4', 'vcodec': 'hvc1.1.6.L120', 'acodec': 'none', 'width': 1920, 'height': 822,
     'fps': 30, 'tbr': 1044, 'filesize_approx': 47.98 * MIB},
    {'format_id': '100026', 'ext': 'mp4', 'vcodec': 'av01.0.08M.08', 'acodec': 'none', 'width': 1920, 'height': 822,
     'fps': 30, 'tbr': 1013, 'filesize_approx': 46.53 * MIB},
]


class StreamCopyTests(unittest.IsolatedAsyncioTestCase):
    def test_selects_largest_h264_aac_pair_within_budget(self):
        chosen = clip_query.select_copy_formats(BILIBILI_FORMATS, 385.45, 100)
        self.assertEqual(chosen['format'], '30064+30280')
        self.assertEqual((chosen['width'], chosen['height']), (1280, 548))
        self.assertLessEqual(chosen['estimated_size_mb'], 95)
        self.assertEqual(clip_query.select_copy_formats(BILIBILI_FORMATS, 385.45, 45)['format'], '30032+30216')
        self.assertIsNone(clip_query.select_copy_formats(BILIBILI_FORMATS, 385.45, 10))
        # HEVC/AV1 are not copied even though they are smaller; they are not reliably playable inline.
        self.assertIsNone(clip_query.select_copy_formats(
            [f for f in BILIBILI_FORMATS if not f['vcodec'].startswith('avc1')], 385.45, 100))

    async def test_create_clip_stream_copies_fitting_source_and_falls_back_to_encoding(self):
        if not shutil.which('ffmpeg'):
            self.skipTest('ffmpeg is unavailable')
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'fixture.mp4'
            await asyncio.to_thread(subprocess.run, ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc=size=320x180:rate=30:duration=2',
                            '-f', 'lavfi', '-i', 'sine=duration=2', '-c:v', 'libx264', '-c:a', 'aac', '-shortest',
                            str(fixture)], check=True)
            formats = [{'format_id': 'v', 'ext': 'mp4', 'vcodec': 'avc1.64000d', 'acodec': 'none', 'width': 320,
                        'height': 180, 'fps': 30, 'filesize': fixture.stat().st_size},
                       {'format_id': 'a', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a.40.2', 'abr': 64, 'filesize': 1}]
            requested = []

            class FakeYoutubeDL:
                def __init__(self, options):
                    self.options = options
                def __enter__(self):
                    return self
                def __exit__(self, *error):
                    return False
                def extract_info(self, url, download):
                    assert download is False
                    return {'formats': formats, 'ext': 'mp4'}
                def process_ie_result(self, info, download):
                    requested.append(self.options['format'])
                    shutil.copy(fixture, self.prepare_filename(info))
                    return info
                def prepare_filename(self, info):
                    return self.options['outtmpl'].replace('%(ext)s', 'mp4')

            def clip(budget):
                return clip_query.ClipSpec(url='https://example.com/v', start=0, end=2, output_format='mp4',
                                           resolution='180p', bitrate='200k', fps=30, copy_budget_mb=budget)
            with patch.dict('sys.modules', {'yt_dlp': SimpleNamespace(YoutubeDL=FakeYoutubeDL)}):
                copied = clip(10)
                output = Path(directory) / 'copied.mp4'
                self.assertTrue(await clip_query.create_clip(copied, str(output)))
                self.assertEqual(copied.stream_copy['format'], 'v+a')
                self.assertEqual(output.stat().st_size // 1024, fixture.stat().st_size // 1024)
                encoded = clip(None)
                self.assertTrue(await clip_query.create_clip(encoded, str(Path(directory) / 'encoded.mp4')))
                self.assertIsNone(encoded.stream_copy)
            self.assertEqual(requested, ['v+a', 'bestvideo+bestaudio/best'])
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['copied.mp4', 'encoded.mp4', 'fixture.mp4'])


if __name__ == "__main__":
    unittest.main()

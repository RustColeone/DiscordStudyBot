"""Video download and fitting (ported from providers/clip_query.py and features/feature_clip.py).

Downloads with yt-dlp, keeps the full requested range, and fits the file budget: a fitting H.264/AAC
source is stream-copied; otherwise the video is re-encoded with an adaptive audio bitrate.
"""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

Progress = Optional[Callable[[str], Awaitable[None]]]


class VideoSourceError(RuntimeError):
    """The source could not be inspected or downloaded; encoding settings cannot help."""


@dataclass
class VideoPlan:
    url: str
    start: float
    end: float
    resolution: str
    bitrate: str
    fps: int
    audio_bitrate: str
    copy_budget_mb: Optional[float] = None
    stream_copy: Optional[dict] = field(default=None)


def parse_time(text: str) -> float:
    """Seconds from "65", "1:05", "1:05.5" or "1:02:03"."""
    parts = str(text).strip().split(":")
    if len(parts) > 3:
        raise ValueError(f"Invalid time {text!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def kbps(value: str) -> float:
    return float(str(value).lower().removesuffix("k"))


def estimate_size_mb(duration: float, video_bitrate: str, audio_bitrate: str) -> float:
    return round((kbps(video_bitrate) + kbps(audio_bitrate)) * duration / 8 / 1024, 2)


def fit_budget(duration: float, max_size_mb: float, audio_bitrate: str) -> Optional[tuple[str, str, int]]:
    """Video settings that leave room for audio and muxing, or None below the quality floor."""
    if duration <= 0 or max_size_mb <= 0:
        return None
    video_kbps = min(2500, int(max_size_mb * 0.90 * 8 * 1024 / duration - kbps(audio_bitrate)))
    if video_kbps < 96:
        return None
    resolution = "360p" if video_kbps < 400 else "480p" if video_kbps < 800 else "720p"
    return resolution, f"{video_kbps}k", 24


def _format_bytes(entry: dict, duration: float) -> Optional[float]:
    size = entry.get("filesize") or entry.get("filesize_approx")
    if size:
        return float(size)
    rate = entry.get("tbr") or entry.get("vbr") or entry.get("abr")
    return float(rate) * 1000 / 8 * duration if rate else None


def select_copy_formats(formats: list[dict], duration: float, budget_mb: float, max_height: int = 1080) -> Optional[dict]:
    """The largest H.264 video + AAC audio pair that fits the budget without re-encoding."""
    limit = budget_mb * 0.95 * 1024 * 1024
    videos, audios = [], []
    for entry in formats:
        vcodec, acodec = str(entry.get("vcodec") or "none"), str(entry.get("acodec") or "none")
        if entry.get("ext") not in ("mp4", "m4a") or str(entry.get("protocol") or "https").startswith(("m3u8", "f4m")):
            continue
        size = _format_bytes(entry, duration)
        if size is None:
            continue
        if vcodec.startswith(("avc1", "h264")) and acodec == "none" and (entry.get("height") or 0) <= max_height:
            videos.append((entry, size))
        elif acodec.startswith(("mp4a", "aac")) and vcodec == "none":
            audios.append((entry, size))
    best = None
    for video, video_size in videos:
        for audio, audio_size in audios:
            if video_size + audio_size > limit:
                continue
            rank = (video.get("height") or 0, video.get("tbr") or 0, audio.get("abr") or 0)
            if best is None or rank > best[0]:
                best = (rank, video, audio, video_size + audio_size)
    if best is None:
        return None
    _, video, audio, size = best
    return {"format": f"{video['format_id']}+{audio['format_id']}", "width": video.get("width"),
            "height": video.get("height"), "fps": video.get("fps"), "video_codec": video.get("vcodec"),
            "audio_codec": audio.get("acodec"), "estimated_size_mb": round(size / 1024 / 1024, 2)}


def _ydl_options(extra: dict | None = None) -> dict:
    options = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
               "socket_timeout": 15, "retries": 1, "extractor_retries": 1}
    cookies = os.getenv("MEDIA_COOKIES_FILE", "").strip()
    if cookies and Path(cookies).is_file():
        options["cookiefile"] = cookies
    options.update(extra or {})
    return options


async def video_info(url: str) -> tuple[str, str, float]:
    """(title, site, duration seconds) without downloading."""
    def extract():
        import yt_dlp
        with yt_dlp.YoutubeDL(_ydl_options({"skip_download": True})) as ydl:
            info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        if duration <= 0:
            raise VideoSourceError("The source did not provide a usable video duration")
        return info.get("title") or "video", info.get("extractor_key") or "unknown", float(duration)
    try:
        return await asyncio.to_thread(extract)
    except VideoSourceError:
        raise
    except Exception as error:
        raise VideoSourceError(f"Unable to inspect the video source: {error}") from error


def safe_name(title: str) -> str:
    return (re.sub(r"[^\w\-.]+", "_", title).strip("._") or "video")[:80]


async def render(plan: VideoPlan, output: Path, progress: Progress = None) -> Path:
    """Download and produce `output` (MP4). Raises VideoSourceError or RuntimeError."""
    import yt_dlp
    template = str(output.with_suffix(".source.%(ext)s"))
    base = _ydl_options({"format": "bestvideo+bestaudio/best", "outtmpl": template})
    if progress is not None:
        await progress("Downloading the source video...")

    def download():
        try:
            with yt_dlp.YoutubeDL(base) as ydl:
                info = ydl.extract_info(plan.url, download=False)
            copy = None
            if plan.copy_budget_mb:
                copy = select_copy_formats(info.get("formats") or [], plan.end - plan.start, plan.copy_budget_mb)
            with yt_dlp.YoutubeDL({**base, "format": copy["format"] if copy else base["format"]}) as ydl:
                result = ydl.process_ie_result(info, download=True)
                return ydl.prepare_filename(result), copy
        except Exception as error:
            raise VideoSourceError(f"Unable to download the video source: {error}") from error

    source, copy = await asyncio.to_thread(download)
    try:
        if copy is not None:
            if progress is not None:
                await progress(f"The source {copy['width']}x{copy['height']} H.264/AAC (~{copy['estimated_size_mb']:.1f} MB) "
                               "fits; copying it without re-encoding...")
            code, _ = await _ffmpeg("-i", source, "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                                    "-movflags", "+faststart", "-y", str(output))
            if code == 0 and output.is_file() and output.stat().st_size / 1024 / 1024 <= plan.copy_budget_mb:
                plan.stream_copy = copy
                return output
            if progress is not None:
                await progress("The copied source did not fit; re-encoding instead...")
        if progress is not None:
            await progress(f"Encoding {plan.end - plan.start:g}s at {plan.resolution}, {plan.fps} fps, "
                           f"video {plan.bitrate} + audio {plan.audio_bitrate}...")
        filters = f"scale=-2:{plan.resolution.removesuffix('p')},fps={plan.fps}"
        code, error = await _ffmpeg("-ss", str(plan.start), "-i", source, "-t", str(plan.end - plan.start),
                                    "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", plan.audio_bitrate,
                                    "-vf", filters, "-b:v", plan.bitrate, "-movflags", "+faststart", "-y", str(output))
        if code != 0:
            raise RuntimeError("FFmpeg processing failed: " + error[-600:])
        return output
    finally:
        with suppress(OSError):
            os.remove(source)


async def _ffmpeg(*arguments: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", *arguments,
                                                   stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await process.communicate()
    return process.returncode, stderr.decode(errors="replace")

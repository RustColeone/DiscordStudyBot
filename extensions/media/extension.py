"""Media extension: download a video (full or a range) fitted to the conversation's upload limit."""

from __future__ import annotations

import math

from contracts import Capability, Contributions, Extension, FileRef, Risk, ToolResult

from . import video

AUDIO_CHOICES = ("128k", "96k", "64k")


async def download_video(arguments, context) -> ToolResult:
    url = arguments["url"].strip()
    await context.progress("Inspecting the video source...")
    try:
        title, site, full_duration = await video.video_info(url)
        start = video.parse_time(arguments["start"]) if arguments.get("start") else 0.0
        end = min(video.parse_time(arguments["end"]), full_duration) if arguments.get("end") else full_duration
    except video.VideoSourceError as error:
        return ToolResult.fail(str(error), "source_unavailable")
    except ValueError as error:
        return ToolResult.fail(str(error), "invalid_arguments")
    if start < 0 or end <= start:
        return ToolResult.fail("The requested video range is invalid.", "invalid_range")
    limit = float(context.display.max_upload_mb)
    requested = float(arguments.get("max_size_mb") or limit)
    if not math.isfinite(requested) or requested <= 0:
        return ToolResult.fail("The requested maximum file size must be positive.", "invalid_arguments")
    budget = min(limit, requested)
    source = "user_target" if requested < limit else "conversation_limit"
    duration = end - start
    fit, audio = None, None
    for audio in ([arguments["audio_bitrate"]] if arguments.get("audio_bitrate") else AUDIO_CHOICES):
        fit = video.fit_budget(duration, budget, audio)
        if fit is not None:
            break
    if fit is None:
        return ToolResult.ask(
            f"{duration:g}s of video cannot fit the {budget:g} MB file budget ({source}) at a usable quality. "
            "Nothing was downloaded or sent. Ask whether a shorter range is fine.",
            error_code="compression_budget", data={"budget_mb": budget, "budget_source": source})
    resolution, bitrate, fps = fit
    explicit = any(arguments.get(key) for key in ("fps", "resolution", "bitrate", "audio_bitrate"))
    plan = video.VideoPlan(url, start, end, arguments.get("resolution") or resolution,
                           arguments.get("bitrate") or bitrate, int(arguments.get("fps") or fps), audio,
                           copy_budget_mb=budget if start == 0 and end >= full_duration and not explicit else None)
    output = context.workspace / f"{video.safe_name(title)}.mp4"
    try:
        await video.render(plan, output, context.progress)
    except video.VideoSourceError as error:
        return ToolResult.fail(str(error), "source_unavailable")
    except RuntimeError as error:
        return ToolResult.fail(str(error), "render_failed")
    size_mb = output.stat().st_size / 1024 / 1024
    if size_mb > budget:
        output.unlink(missing_ok=True)
        return ToolResult.fail(f"The result was {size_mb:.2f} MB, above the {budget:g} MB budget; it was deleted. "
                               "A shorter range or a lower quality would fit.", "output_budget_exceeded")
    if plan.stream_copy:
        copied = plan.stream_copy
        settings = f"{copied['width']}x{copied['height']}, {copied['fps']} fps, source H.264/AAC copied without re-encoding"
        method = "stream_copy"
    else:
        settings = f"{plan.resolution}, {plan.fps} fps, video {plan.bitrate} + audio {plan.audio_bitrate}"
        method = "reencode"
    return ToolResult.ok(
        f"Prepared \"{title}\" ({site}): {duration:g}s, {size_mb:.2f} MB, {settings}; budget {budget:g} MB ({source}).",
        files=(FileRef(str(output), output.name, "video/mp4", output.stat().st_size),), sources=(url,),
        data={"method": method, "duration_s": duration, "size_mb": round(size_mb, 2), "budget_mb": budget})


def setup(context) -> Contributions:
    return Contributions(capabilities=[Capability(
        "media.download_video",
        "Download a video from a link (YouTube, Bilibili and most sites) and deliver it as a file. The full video is "
        "kept unless the user asks for a range. It is fitted to this conversation's upload limit automatically.",
        download_video,
        parameters={
            "url": {"type": "string", "description": "Video page URL"},
            "start": {"type": "string", "description": "Optional start, e.g. 1:05"},
            "end": {"type": "string", "description": "Optional end, e.g. 2:30"},
            "max_size_mb": {"type": "number", "minimum": 0.1, "description": "Optional smaller size the user asked for"},
            "resolution": {"type": "string", "enum": ["360p", "480p", "720p", "1080p"]},
            "fps": {"type": "integer", "minimum": 1, "maximum": 60},
            "bitrate": {"type": "string", "pattern": "^[0-9]{2,5}k$"},
            "audio_bitrate": {"type": "string", "pattern": "^(32|48|64|96|128|160|192|256|320)k$"},
        },
        required=("url",), risk=Risk.PREPARE, needs={"files"}, command="video", command_argument="url",
        timeout_s=1800)])


EXTENSION = Extension("media", setup, contracts="1", version="1", description="Video downloads fitted to upload limits")

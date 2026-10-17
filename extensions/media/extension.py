"""Media extension: video downloads and clips fitted to the conversation's upload limit."""

from __future__ import annotations

import math
from pathlib import Path

from contracts import Capability, Command, Contributions, Extension, FileRef, Option, Risk, ToolResult

from . import clip, video

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


QUALITY = {
    "resolution": {"type": "string", "enum": ["360p", "480p", "720p", "1080p"]},
    "fps": {"type": "integer", "minimum": 1, "maximum": 60},
    "bitrate": {"type": "string", "pattern": "^[1-9][0-9]{1,4}k$",
                "description": "Video bitrate, or the audio bitrate for audio formats"},
    "format": {"type": "string", "enum": list(clip.FORMATS), "description": "Output format (default mp4)"},
    "keep_file": {"type": "boolean", "description": "Also keep the rendered file on the bot host"},
}


def clip_capabilities(keep_dir: Path) -> list[Capability]:
    async def clip_video(arguments, context) -> ToolResult:
        try:
            clips = await clip.create(context, [(arguments["url"].strip(), arguments.get("start"), arguments.get("end"))],
                                      arguments.get("format") or "mp4", arguments.get("resolution"),
                                      arguments.get("fps"), arguments.get("bitrate"), bool(arguments.get("keep_file")))
        except clip.ClipError as error:
            return ToolResult.fail(str(error), error.code)
        clip.save(context, clips)
        return clip.preview_result(context, clips, commands=False)

    async def adjust_pending_clip(arguments, context) -> ToolResult:
        limit = float(context.display.max_upload_mb)
        action, note = arguments["action"], ""
        try:
            clips, index = clip.pick(context, arguments.get("clip"))
            item = clips[index]
            if action == "trim":
                if not (arguments.get("start") or arguments.get("end")):
                    return ToolResult.fail("Trimming needs a start or an end time.", "invalid_arguments")
                clip.trim(item, arguments.get("start"), arguments.get("end"))
            elif action == "fit_upload":
                if not clip.fit(item, limit, arguments.get("resolution")):
                    return ToolResult.ask(
                        f"{clip.duration_of(item):g}s is too long to fit the {limit:g} MB upload limit at usable "
                        "quality. Nothing was changed. Ask for a shorter start and end time.",
                        error_code="compression_budget", data={"limit_mb": limit})
            elif action == "keep_full_quality":
                item["keep_file"] = True
                if item["estimated_size_mb"] > limit:
                    note = ("⚠️ This remains too large to upload here. It can be processed and kept on the bot host, "
                            "but trimming or compression is required for delivery in this conversation.")
            elif not clip.set_quality(item, limit, arguments.get("resolution"), arguments.get("fps"),
                                      arguments.get("bitrate"), arguments.get("format"), arguments.get("keep_file")):
                return ToolResult.fail("set_quality needs resolution, fps, bitrate, format or keep_file.",
                                       "invalid_arguments")
        except clip.ClipError as error:
            return ToolResult.fail(str(error), error.code)
        clip.save(context, clips)
        return clip.preview_result(context, clips, commands=False, note=note)

    async def manage_pending_clips(arguments, context) -> ToolResult:
        if arguments["action"] == "cancel":
            return clip.cancel(context)
        return await clip.confirm(context, keep_dir, arguments.get("skip") or (), arguments.get("keep_file"))

    async def list_pending_clips(arguments, context) -> ToolResult:
        clips = clip.load(context)
        if not clips:
            return ToolResult.ok("There are no pending clips in this conversation.", data={"clips": []})
        limit = float(context.display.max_upload_mb)
        return ToolResult.ok(clip.preview_text(clips, limit, context.display.markdown, commands=False),
                             data={"limit_mb": limit, "clips": [clip.summary(item) for item in clips]})

    async def clip_command(arguments, context) -> ToolResult:
        return await clip.command(context, arguments.get("text", ""), keep_dir)

    return [
        Capability(
            "media.clip_video",
            "Inspect a video URL and create a reversible pending clip preview with size estimates for this "
            "conversation's upload limit; nothing is rendered yet. Resolve 'this video' from recent conversation. "
            "If no time range or delivery goal is given, prepare the preview first; when it is too large, ask whether "
            "to trim, compress to the upload limit, or keep full quality on the bot host. For a plain download use "
            "media.download_video instead.",
            clip_video,
            parameters={"url": {"type": "string", "description": "Video page URL"},
                        "start": {"type": "string", "description": "Optional start, e.g. 1:05"},
                        "end": {"type": "string", "description": "Optional end, e.g. 1:20"}, **QUALITY},
            required=("url",), risk=Risk.PREPARE, needs={"files"}, timeout_s=300),
        Capability(
            "media.adjust_pending_clip",
            "Update a pending clip after the person states the goal: trim (start and/or end), fit_upload (compress "
            "the current range under this conversation's upload limit), keep_full_quality (keep the current quality "
            "and keep the file on the bot host when it cannot be uploaded), or set_quality (explicit resolution, "
            "fps, bitrate, format or keep_file). Returns the updated preview.",
            adjust_pending_clip,
            parameters={"action": {"type": "string", "enum": ["trim", "fit_upload", "keep_full_quality", "set_quality"],
                                   "description": "How to adapt the pending clip"},
                        "clip": {"type": "integer", "minimum": 1, "description": "One-based clip number (default 1)"},
                        "start": {"type": "string", "description": "Trim start, e.g. 1:05"},
                        "end": {"type": "string", "description": "Trim end, e.g. 1:20"}, **QUALITY},
            required=("action",), risk=Risk.PREPARE),
        Capability(
            "media.manage_pending_clips",
            "Confirm the pending clips (render them and deliver the files to this conversation) or cancel the "
            "pending preview. The destination is the current conversation.",
            manage_pending_clips,
            parameters={"action": {"type": "string", "enum": ["confirm", "cancel"]},
                        "skip": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                 "description": "One-based clip numbers to leave out when confirming"},
                        "keep_file": QUALITY["keep_file"]},
            required=("action",), risk=Risk.PREPARE, needs={"files"}, timeout_s=1800),
        Capability(
            "media.list_pending_clips",
            "Show this conversation's pending clips (settings, size estimates and the upload limit).",
            list_pending_clips, risk=Risk.READ),
        Capability(
            "media.clip_command",
            "The $clip command: preview, adjust, confirm or cancel clips with legacy flags.",
            clip_command, parameters={"text": {"type": "string"}}, risk=Risk.PREPARE, internal=True,
            needs={"files"}, command=CLIP_COMMAND, command_argument="text", timeout_s=1800),
    ]


CLIP_COMMAND = Command(
    "clip", "Preview a clip of a video with size estimates, then confirm to send it",
    (Option("url", "Video link (leave empty to adjust, confirm or list pending clips)"),
     Option("start", "Start, e.g. 1:05"), Option("end", "End, e.g. 1:20"),
     Option("resolution", "Resolution", choices=("360p", "480p", "720p", "1080p")),
     Option("format", "Output format", choices=("mp4", "webm", "mov", "mkv", "gif", "mp3", "m4a", "wav", "aac",
                                                 "ogg", "flac")),
     Option("clip", "Which pending clip to adjust (1, 2, …)", "integer"),
     Option("fit", "Shrink it to fit the upload limit", "boolean", flag="--fit"),
     Option("keep_file", "Also keep the full-quality file on the host", "boolean", flag="--keep-file"),
     Option("confirm", "Render and send the pending clips", "boolean", flag="--confirm"),
     Option("cancel", "Discard the pending clips", "boolean", flag="--cancel")),
    template="[-u {url}][ -s {start}][ -e {end}][ --clip {clip}][ -r {resolution}][ -f {format}][ {fit}]"
             "[ {keep_file}][ {confirm}][ {cancel}]",
    examples=("-u https://youtu.be/… -s 1:05 -e 1:20", "--confirm", "--clip 1 --fit", "--cancel"))


def setup(context) -> Contributions:
    keep_dir = Path(str(context.config.get("keep_dir") or "clips")).expanduser()
    if not keep_dir.is_absolute():
        keep_dir = context.data_dir / keep_dir
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
        required=("url",), risk=Risk.PREPARE, needs={"files"},
        command=Command("video", "Download a video, fitted to the upload limit",
                        (Option("url", "Video page link", required=True),), message_action="Download video"),
        command_argument="url",
        timeout_s=1800), *clip_capabilities(keep_dir)])


EXTENSION = Extension("media", setup, contracts="1", version="1",
                      description="Video downloads and clips fitted to upload limits")

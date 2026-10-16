"""Pending video clips (ported from features/feature_clip.py, providers/clip_query.py, the $clip parser and the
agent tools clip_video, adjust_pending_clip and manage_pending_clips).

A clip request first becomes a pending preview with size estimates for the conversation's upload limit. It can be
trimmed, fitted or re-tuned, then confirmed (rendered and delivered) or cancelled. Pending clips are kept in the
extension store under `pending:<address>`, so a preview survives restarts.
"""

from __future__ import annotations

import mimetypes
import re
import shlex
import shutil
import time
from pathlib import Path

from contracts import FileRef, ToolResult

from . import video

VIDEO_PRESETS = (("1080p", "2500k", 30), ("720p", "1500k", 30), ("480p", "1000k", 30), ("360p", "600k", 30))
GIF_PRESETS = (("720p", "1000k", 15), ("480p", "600k", 15), ("360p", "400k", 10))
AUDIO_PRESETS = ("320k", "192k", "128k")
FIT_AUDIO = ("128k", "96k", "64k")
FORMATS = ("mp4", "webm", "mov", "mkv", "gif", *video.AUDIO_FORMATS)
LABELS = "ABCDE"
RESOLUTION = re.compile(r"^[0-9]{3,4}p$")
BITRATE = re.compile(r"^[1-9][0-9]{1,4}k$")


class ClipError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# ---- sizes and options ----------------------------------------------------------------------------
def kind(output_format: str) -> str:
    return "audio" if output_format in video.AUDIO_FORMATS else "gif" if output_format == "gif" else "video"


def estimate(duration: float, output_format: str, bitrate: str, audio_bitrate: str = "128k") -> float:
    """MB for the clip; GIFs carry no audio and audio clips only their own bitrate."""
    if kind(output_format) == "video":
        return video.estimate_size_mb(duration, bitrate, audio_bitrate)
    return round(video.kbps(bitrate) * duration / 8 / 1024, 2)


def quality_options(duration: float, limit: float, output_format: str) -> list[dict]:
    """Suggested settings from highest to lowest quality (audio options are limited to those that fit)."""
    if kind(output_format) == "audio":
        options = [{"label": LABELS[i], "resolution": "audio", "bitrate": rate, "fps": 0,
                    "estimated_size_mb": estimate(duration, output_format, rate)} for i, rate in enumerate(AUDIO_PRESETS)]
        return [option for option in options if option["estimated_size_mb"] <= limit] or [options[-1]]
    presets = GIF_PRESETS if output_format == "gif" else VIDEO_PRESETS
    return [{"label": LABELS[i], "resolution": resolution, "bitrate": rate, "fps": fps,
             "estimated_size_mb": estimate(duration, output_format, rate)}
            for i, (resolution, rate, fps) in enumerate(presets)]


def format_time(seconds: float) -> str:
    minutes, rest = int(seconds // 60), seconds % 60
    return f"{minutes}:{int(rest):02d}" if rest == int(rest) else f"{minutes}:{rest:05.2f}"


def duration_of(clip: dict) -> float:
    return clip["end"] - clip["start"]


def refresh(clip: dict) -> dict:
    clip["estimated_size_mb"] = estimate(duration_of(clip), clip["format"], clip["bitrate"], clip["audio_bitrate"])
    return clip


def apply_defaults(clip: dict, limit: float, resolution=None, fps=None, bitrate=None) -> dict:
    """Settings of the best quality option unless given (as the legacy preview did)."""
    best = quality_options(duration_of(clip), limit, clip["format"])[0]
    audio = kind(clip["format"]) == "audio"
    clip.update(resolution="audio" if audio else resolution or best["resolution"],
                fps=0 if audio else int(fps or best["fps"]), bitrate=bitrate or best["bitrate"])
    clip["audio_bitrate"] = clip["bitrate"] if audio else "128k"
    return refresh(clip)


def fit(clip: dict, limit: float, resolution: str | None = None) -> bool:
    """Compress the clip's current range under `limit`; False when no usable quality fits."""
    duration, output_format = duration_of(clip), clip["format"]
    if kind(output_format) == "video":
        for audio in FIT_AUDIO:
            settings = video.fit_budget(duration, limit, audio)
            if settings is not None:
                clip.update(resolution=resolution or settings[0], bitrate=settings[1], fps=settings[2],
                            audio_bitrate=audio)
                refresh(clip)
                return True
        return False
    for option in quality_options(duration, limit, output_format):
        if option["estimated_size_mb"] <= limit:
            clip.update(resolution=option["resolution"], bitrate=option["bitrate"], fps=option["fps"])
            if kind(output_format) == "audio":
                clip["audio_bitrate"] = option["bitrate"]
            refresh(clip)
            return True
    return False


# ---- pending state --------------------------------------------------------------------------------
def _key(context) -> str:
    return "pending:" + str(context.address)


def load(context) -> list[dict]:
    value = context.storage.get(_key(context))
    return [dict(clip) for clip in (value or {}).get("clips", [])]


def save(context, clips: list[dict]) -> None:
    if clips:
        context.storage.set(_key(context), {"clips": clips, "updated_at": time.time()})
    else:
        context.storage.delete(_key(context))


def summary(clip: dict) -> dict:
    return {"title": clip["title"], "start": clip["start"], "end": clip["end"], "duration": duration_of(clip),
            "resolution": clip["resolution"], "bitrate": clip["bitrate"], "fps": clip["fps"],
            "audio_bitrate": clip["audio_bitrate"], "format": clip["format"],
            "estimated_size_mb": clip["estimated_size_mb"], "keep_file": clip["keep_file"],
            "range_explicit": clip["range_explicit"]}


def _data(clips: list[dict], limit: float) -> dict:
    return {"limit_mb": limit, "clips": [summary(clip) for clip in clips]}


# ---- text -----------------------------------------------------------------------------------------
def _bold(text: str, markdown: bool) -> str:
    return f"**{text}**" if markdown else text


def _code(text: str, markdown: bool) -> str:
    return f"`{text}`" if markdown else text


def settings_text(clip: dict) -> str:
    if kind(clip["format"]) == "audio":
        return f"{clip['format']} audio @ {clip['bitrate']}"
    if clip["format"] == "gif":
        return f"GIF {clip['resolution']} ({clip['fps']}fps)"
    return f"{clip['resolution']} @ {clip['bitrate']} ({clip['fps']}fps), audio {clip['audio_bitrate']}" + (
        "" if clip["format"] == "mp4" else f", {clip['format']}")


def options_lines(clip: dict, limit: float, indent: str = "") -> list[str]:
    lines = []
    for option in quality_options(duration_of(clip), limit, clip["format"]):
        mark = "✅" if option["estimated_size_mb"] <= limit else "❌"
        quality = (option["bitrate"] if option["resolution"] == "audio"
                   else f"{option['resolution']} @ {option['bitrate']} ({option['fps']}fps)")
        lines.append(f"{indent}{option['label']}) {quality} → ~{option['estimated_size_mb']:.2f}MB {mark}")
    return lines


PREVIEW_COMMANDS = (("$clip --confirm", "Process all clips"),
                    ("$clip --clip <N> --resolution 720p", "Adjust clip N"),
                    ("$clip --clip <N> --fit", "Compress clip N to fit the upload limit"),
                    ("$clip --clip <N> -s <start> -e <end>", "Trim clip N"),
                    ("$clip --confirm --skip <N>", "Skip clip N"),
                    ("$clip --keep-file", "Keep generated files after sending"),
                    ("$clip --delete-file", "Delete generated files after sending"),
                    ("$clip --cancel", "Cancel all"))


def preview_text(clips: list[dict], limit: float, markdown: bool, commands: bool) -> str:
    lines = [f"📊 {_bold('Clip Preview', markdown)} (upload limit: {limit:g}MB)", ""]
    for index, clip in enumerate(clips, start=1):
        size = clip["estimated_size_mb"]
        mark = "✅" if size <= limit else "⚠️"
        lines += [f"{_bold(f'Clip {index}:', markdown)} {mark}",
                  f"Source: {clip['title']} - {clip['site']}",
                  f"Duration: {duration_of(clip):.1f}s ({format_time(clip['start'])} → {format_time(clip['end'])})",
                  f"Selected: {settings_text(clip)}",
                  f"Estimated: {_bold(f'{size:.2f}MB', markdown)}",
                  f"Keep file after send: {'yes' if clip['keep_file'] else 'no'}",
                  "Options:", *options_lines(clip, limit, "  "), ""]
    if commands:
        lines.append(_bold("Commands:", markdown))
        lines += [f"{_code(usage, markdown)} - {meaning}" for usage, meaning in PREVIEW_COMMANDS]
    return "\n".join(lines).rstrip()


def preview_result(context, clips: list[dict], commands: bool, note: str = "") -> ToolResult:
    """The preview; for engines an oversized clip asks for the person's goal first (legacy goal question)."""
    limit = float(context.display.max_upload_mb)
    text = preview_text(clips, limit, context.display.markdown, commands) + (f"\n\n{note}" if note else "")
    data = _data(clips, limit)
    if commands:
        return ToolResult.ok(text, data=data)
    oversized = next((clip for clip in clips if clip["estimated_size_mb"] > limit), None)
    if oversized is None:
        return ToolResult.ok(text + "\n\nNothing is rendered yet. Confirm with media.manage_pending_clips "
                             "(action confirm) or adjust with media.adjust_pending_clip.", data=data)
    if oversized["keep_file"]:
        question = (f"The clip stays at its current quality (~{oversized['estimated_size_mb']:.2f} MB); it will be kept "
                    "on the bot host instead of being uploaded. Ask whether to start processing.")
    else:
        subject = "This clip" if oversized["range_explicit"] else "The full video"
        question = (f"{subject} is about {oversized['estimated_size_mb']:.2f} MB, above this conversation's "
                    f"{limit:g} MB upload limit. Ask whether to trim it (start and end times), compress it to fit "
                    "(adjust_pending_clip fit_upload), or keep the current quality and only keep the file on the "
                    "bot host (keep_full_quality).")
    return ToolResult.ask(text + "\n\n" + question, error_code="clip_goal_needed", data=data)


# ---- operations -----------------------------------------------------------------------------------
async def create(context, sources: list[tuple[str, str | None, str | None]], output_format: str = "mp4",
                 resolution=None, fps=None, bitrate=None, keep_file: bool = False) -> list[dict]:
    """Inspect each (url, start, end) and build pending clips. Raises ClipError."""
    limit = float(context.display.max_upload_mb)
    await context.progress("Inspecting the video source...")
    clips = []
    for url, start_text, end_text in sources:
        try:
            title, site, full_duration = await video.video_info(url)
        except video.VideoSourceError as error:
            raise ClipError(f"Error processing URL {url}: {error}", "source_unavailable") from error
        try:
            start = video.parse_time(start_text) if start_text else 0.0
            end = min(video.parse_time(end_text), full_duration) if end_text else full_duration
        except ValueError as error:
            raise ClipError(str(error), "invalid_arguments") from error
        if start < 0 or end <= start:
            raise ClipError(f"End time must be after start time for URL: {url}", "invalid_range")
        clip = {"url": url, "title": title, "site": site, "source_duration": full_duration, "start": start,
                "end": end, "format": output_format, "keep_file": bool(keep_file),
                "range_explicit": bool(start_text or end_text)}
        clips.append(apply_defaults(clip, limit, resolution, fps, bitrate))
    return clips


def pick(context, number: int | None) -> tuple[list[dict], int]:
    clips = load(context)
    index = (number or 1) - 1
    if not clips:
        raise ClipError("There are no pending clips in this conversation.", "no_pending_clip")
    if not 0 <= index < len(clips):
        raise ClipError(f"Invalid clip number. There are {len(clips)} pending clip(s).", "no_pending_clip")
    return clips, index


def trim(clip: dict, start_text: str | None, end_text: str | None) -> None:
    try:
        start = video.parse_time(start_text) if start_text else clip["start"]
        end = video.parse_time(end_text) if end_text else clip["end"]
    except ValueError as error:
        raise ClipError(str(error), "invalid_arguments") from error
    end = min(end, clip.get("source_duration") or end)
    if start < 0 or end <= start:
        raise ClipError("End time must be after start time.", "invalid_range")
    clip.update(start=start, end=end, range_explicit=True)
    refresh(clip)


def set_quality(clip: dict, limit: float, resolution=None, fps=None, bitrate=None, output_format=None,
                keep_file=None) -> list[str]:
    """Apply explicit settings; returns the changes. A new kind of output starts from its best option."""
    changes = []
    if output_format and output_format != clip["format"]:
        changed_kind = kind(output_format) != kind(clip["format"])
        clip["format"] = output_format
        if changed_kind:
            apply_defaults(clip, limit)
        changes.append(f"format → {output_format}")
    if kind(clip["format"]) == "audio":
        if bitrate:
            clip.update(bitrate=bitrate, audio_bitrate=bitrate)
            changes.append(f"bitrate → {bitrate}")
    else:
        for name, value in (("resolution", resolution), ("fps", fps), ("bitrate", bitrate)):
            if value:
                clip[name] = int(value) if name == "fps" else value
                changes.append(f"{name} → {value}")
    if keep_file is not None:
        clip["keep_file"] = bool(keep_file)
        changes.append(f"keep-file → {bool(keep_file)}")
    refresh(clip)
    return changes


def updated_text(clip: dict, number: int, changes: list[str], limit: float, markdown: bool) -> str:
    size = clip["estimated_size_mb"]
    mark = "✅" if size <= limit else "⚠️"
    lines = [f"📊 Updated Clip {number}:"]
    if changes:
        lines.append(f"Changes: {', '.join(changes)}")
    lines += [f"Settings: {settings_text(clip)}",
              f"{mark} {_bold(f'Estimated size: {size:.2f}MB', markdown)} (limit: {limit:g}MB)", "",
              "Quality options:", *options_lines(clip, limit)]
    return "\n".join(lines)


async def process(context, clips: list[dict], keep_dir: Path) -> ToolResult:
    """Render clips and return the ones within the upload limit as files (legacy _build_clip_file_responses)."""
    limit = float(context.display.max_upload_mb)
    total = len(clips)
    await context.progress(f"🎬 Processing {total} clip(s)... (this may take some time)")
    files, lines, failures, results = [], [], [], []
    for index, clip in enumerate(clips, start=1):
        output = context.workspace / f"{video.safe_name(clip['title'])}_{index}.{clip['format']}"
        plan = video.VideoPlan(clip["url"], clip["start"], clip["end"], clip["resolution"], clip["bitrate"],
                               int(clip["fps"] or 0), clip["audio_bitrate"], output_format=clip["format"])
        try:
            await video.render(plan, output, context.progress)
        except video.VideoSourceError as error:
            failures.append("source_unavailable")
            lines.append(f"❌ Error processing clip {index}: {error}")
            continue
        except RuntimeError as error:
            output.unlink(missing_ok=True)
            failures.append("render_failed")
            lines.append(f"❌ Error processing clip {index}: {error}")
            continue
        if not output.is_file():
            failures.append("render_failed")
            lines.append(f"❌ Failed to process clip {index}")
            continue
        size = output.stat().st_size
        size_mb = size / 1024 / 1024
        kept = None
        if clip["keep_file"]:
            keep_dir.mkdir(parents=True, exist_ok=True)
            kept = keep_dir / f"{video.safe_name(clip['title'])}_{context.run_id}_{index}.{clip['format']}"
            shutil.copy2(output, kept)
        results.append({"clip": index, "size_mb": round(size_mb, 2), "kept_path": str(kept) if kept else None,
                        "attached": size_mb <= limit})
        if size_mb > limit:
            failures.append("output_budget_exceeded")
            if kept:
                lines.append(f"📹 Clip {index}/{total}: {clip['title']} ({size_mb:.2f}MB, kept on host at {kept}). "
                             f"The result exceeds the {limit:g}MB upload limit, so it was not attached.")
            else:
                output.unlink(missing_ok=True)
                lines.append(f"❌ Clip {index} rendered at {size_mb:.2f}MB, above the {limit:g}MB upload limit. "
                             "It was deleted; adjust compression or the requested range before another attempt.")
            continue
        name = f"clip_{index}.{clip['format']}"
        files.append(FileRef(str(output), name, mimetypes.guess_type(name)[0] or "application/octet-stream", size))
        lines.append(f"📹 Clip {index}/{total}: {clip['title']} ({size_mb:.2f}MB)" + (" (kept)" if kept else ""))
    if not failures:
        lines.append("✅ All clips processed")
    data = {"clips": results, "limit_mb": limit}
    sources = tuple(dict.fromkeys(clip["url"] for clip in clips))
    if not files:
        return ToolResult.fail("\n".join(lines), failures[0], sources=sources, data=data)
    return ToolResult.ok("\n".join(lines), files=tuple(files), sources=sources, data=data)


async def confirm(context, keep_dir: Path, skip=(), keep_file=None) -> ToolResult:
    pending = load(context)
    if not pending:
        return ToolResult.fail("No pending clips to process.", "no_pending_clip")
    skipped = {int(number) - 1 for number in skip}
    clips = [dict(clip) for index, clip in enumerate(pending) if index not in skipped]
    if not clips:
        return ToolResult.fail("All clips were skipped.", "invalid_arguments")
    if keep_file is not None:
        for clip in clips:
            clip["keep_file"] = bool(keep_file)
    result = await process(context, clips, keep_dir)
    # Another run may have created new pending clips meanwhile; only clear the ones processed here.
    if load(context) == pending:
        save(context, [])
    return result


def cancel(context) -> ToolResult:
    count = len(load(context))
    save(context, [])
    return ToolResult.ok(f"🗑️ Cancelled {count} pending clip(s)" if count else "There were no pending clips.",
                         data={"cancelled": count})


# ---- $clip ----------------------------------------------------------------------------------------
def parse_command(text: str) -> dict:
    """Flags of the legacy $clip command (plus --fit); see COMMAND_HELP."""
    command = {"urls": [], "starts": [], "ends": [], "resolution": None, "fps": None, "bitrate": None,
               "format": None, "force": False, "confirm": False, "cancel": False, "fit": False,
               "keep_file": None, "clip": None, "skip": [], "errors": []}
    errors = command["errors"]
    try:
        tokens = shlex.split(text.strip())
    except ValueError as error:
        errors.append(f"Could not read the command: {error}")
        return command
    values = {"--url": "urls", "-u": "urls", "--start": "starts", "-s": "starts", "--end": "ends", "-e": "ends",
              "--resolution": "resolution", "-r": "resolution", "--bitrate": "bitrate", "-b": "bitrate",
              "--format": "format", "-f": "format", "--fps": "fps", "--clip": "clip", "--skip": "skip"}
    switches = {"--force": ("force", True), "--confirm": ("confirm", True), "--cancel": ("cancel", True),
                "--fit": ("fit", True), "--keep-file": ("keep_file", True), "--delete-file": ("keep_file", False)}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in switches:
            name, value = switches[token]
            command[name] = value
            index += 1
            continue
        if token not in values:
            errors.append(f"Unknown flag: {token}")
            index += 1
            continue
        if index + 1 >= len(tokens):
            errors.append(f"{token} requires a value")
            index += 1
            continue
        name, value = values[token], tokens[index + 1]
        index += 2
        if name in ("fps", "clip", "skip"):
            if not value.isdecimal() or int(value) < 1:
                errors.append(f"{token} requires a positive number")
                continue
            value = int(value)
        if name == "resolution" and not RESOLUTION.match(value):
            errors.append("--resolution must look like 720p")
        elif name == "bitrate" and not BITRATE.match(value):
            errors.append("--bitrate must look like 1500k")
        elif name == "format" and value.lower() not in FORMATS:
            errors.append(f"--format must be one of {', '.join(FORMATS)}")
        elif name == "fps" and value > 60:
            errors.append("--fps must be at most 60")
        elif isinstance(command[name], list):
            command[name].append(value)
        else:
            command[name] = value.lower() if name == "format" else value
    return command


COMMAND_HELP = ("Usage: $clip -u <url> [-s <start>] [-e <end>] [-u <url2> ...] [--resolution 720p] [--fps 30] "
                "[--bitrate 1500k] [--format mp4|webm|mov|mkv|gif|mp3|m4a|wav|aac|ogg|flac] [--force] [--keep-file]\n"
                "Then: $clip --confirm [--skip <N>] · $clip --cancel · $clip --clip <N> [--resolution ...] "
                "[--fit] [-s <start> -e <end>] · $clip (show pending clips)")


async def command(context, text: str, keep_dir: Path) -> ToolResult:
    """$clip: preview, adjust, confirm or cancel pending clips (legacy ClipFeature.handle)."""
    parsed = parse_command(text or "")
    if parsed["errors"]:
        return ToolResult.fail("❌ " + "\n".join(parsed["errors"]) + "\n" + COMMAND_HELP, "invalid_arguments")
    limit = float(context.display.max_upload_mb)
    try:
        if parsed["cancel"]:
            return cancel(context)
        if parsed["confirm"]:
            return await confirm(context, keep_dir, parsed["skip"], parsed["keep_file"])
        quality = {key: parsed[key] for key in ("resolution", "fps", "bitrate")}
        if parsed["urls"]:
            sources = [(url, parsed["starts"][i] if i < len(parsed["starts"]) else None,
                        parsed["ends"][i] if i < len(parsed["ends"]) else None) for i, url in enumerate(parsed["urls"])]
            clips = await create(context, sources, parsed["format"] or "mp4", keep_file=bool(parsed["keep_file"]),
                                 **quality)
            if parsed["fit"]:
                for clip in clips:
                    fit(clip, limit, parsed["resolution"])
            if parsed["force"]:
                return await process(context, clips, keep_dir)
            save(context, clips)
            return preview_result(context, clips, commands=True)
        adjusting = (parsed["clip"] is not None or parsed["fit"] or parsed["keep_file"] is not None
                     or parsed["format"] or parsed["starts"] or parsed["ends"] or any(quality.values()))
        if not adjusting:
            clips = load(context)
            return preview_result(context, clips, commands=True) if clips else ToolResult.ok(
                "No pending clips.\n" + COMMAND_HELP)
        clips, index = pick(context, parsed["clip"])
        only_keep = parsed["keep_file"] is not None and parsed["clip"] is None and not (
            parsed["fit"] or parsed["format"] or parsed["starts"] or parsed["ends"] or any(quality.values()))
        if only_keep:
            for item in clips:
                item["keep_file"] = parsed["keep_file"]
            save(context, clips)
            return preview_result(context, clips, commands=True)
        clip = clips[index]
        changes = []
        if parsed["starts"] or parsed["ends"]:
            trim(clip, (parsed["starts"] or [None])[-1], (parsed["ends"] or [None])[-1])
            changes.append(f"range → {format_time(clip['start'])}-{format_time(clip['end'])}")
        changes += set_quality(clip, limit, output_format=parsed["format"], keep_file=parsed["keep_file"], **quality)
        if parsed["fit"]:
            if not fit(clip, limit, parsed["resolution"]):
                return ToolResult.fail("⚠️ This clip is too long to fit the upload limit at usable video quality. "
                                       "Please choose a shorter start and end time.", "compression_budget")
            changes.append("fitted to the upload limit")
        save(context, clips)
        return ToolResult.ok(updated_text(clip, index + 1, changes, limit, context.display.markdown),
                             data=_data(clips, limit))
    except ClipError as error:
        return ToolResult.fail(f"❌ {error}", error.code)

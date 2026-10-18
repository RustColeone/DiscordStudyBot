"""Host: this computer's desktop and files, for the bot's creator only.

Screenshots, sending a file from the computer, saving attachments onto it, the clipboard, opening a
URL or file with its default app, and what is running. Everything here is creator-only. Looking (the
screen, a file, the clipboard, the process list) is a private read: offered only in the creator's own
private chats, and never for secret files. Saving files onto the computer is critical; opening something
and setting the clipboard are writes. Works on Windows, macOS and Linux; see docs/core/EXTENSIONS.md.
"""

from __future__ import annotations

import asyncio
import platform
import re
import shutil
import time
from pathlib import Path

import psutil

from contracts import Capability, Command, Contributions, Extension, Setting, FileRef, Option, Risk, ToolResult

from .desktop import Desktop, DesktopError, free_name

URL = re.compile(r"^(https?|mailto):", re.I)
SYSTEM = f"{platform.system()} {platform.release()}".strip()


def upload_limit(context) -> int:
    return int(float(context.display.max_upload_mb) * 1024 * 1024)


def fit_image(path: Path, limit: int) -> Path:
    """The screenshot as is, or as a smaller JPEG when the PNG is over the conversation's upload limit."""
    if path.stat().st_size <= limit:
        return path
    from PIL import Image
    image = Image.open(path).convert("RGB")
    target = path.with_suffix(".jpg")
    for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
        resized = image if scale == 1.0 else image.resize((max(1, int(image.width * scale)),
                                                            max(1, int(image.height * scale))))
        resized.save(target, "JPEG", quality=80)
        if target.stat().st_size <= limit:
            return target
    raise DesktopError("The screenshot is too large to send here even when shrunk")


def setup(context) -> Contributions:
    config = context.config
    desktop = Desktop(config.get("desktop_env") or {})
    downloads = Path(str(config.get("downloads_dir") or Path.home() / "Downloads")).expanduser()

    def failed(error: DesktopError) -> ToolResult:
        return ToolResult.fail(str(error), "desktop_unavailable")

    async def screenshot(arguments, tool) -> ToolResult:
        try:
            path = await desktop.screenshot(tool.workspace / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png")
            path = fit_image(path, upload_limit(tool))
        except DesktopError as error:
            return failed(error)
        return ToolResult.ok("Screenshot of the computer's screen.", files=(FileRef(str(path), path.name),))

    async def get_file(arguments, tool) -> ToolResult:
        path = Path(str(arguments["path"]).strip()).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        if not path.is_file():
            return ToolResult.fail(f"No file at {path}" + (" (it is a folder; list it with the terminal)"
                                                             if path.is_dir() else ""), "not_found")
        size = path.stat().st_size
        if size > upload_limit(tool):
            return ToolResult.fail(f"{path.name} is {size / 1048576:.1f} MB; this conversation takes at most "
                                   f"{tool.display.max_upload_mb:g} MB. Compress or split it first.", "too_large")
        copy = tool.workspace / path.name
        shutil.copy2(path, copy)  # a snapshot: the chat and its history keep this version
        return ToolResult.ok(f"{path} ({size / 1024:.0f} KB).", files=(FileRef(str(copy), path.name),))

    async def save_files(arguments, tool) -> ToolResult:
        # Only the creator's own attachments (in a group others may add messages to the run), and only
        # the ones named, so the approval shows exactly what will be written.
        own = list(tool.own_files)
        if not own:
            return ToolResult.fail("Attach the file(s) to your own message that asks to save them.", "no_files")
        names = [str(name).strip() for name in arguments.get("names") or [] if str(name).strip()]
        available = [file.name or Path(file.path).name for file in own]
        chosen = own if not names else [file for file, name in zip(own, available) if name in names]
        missing = sorted(set(names) - set(available))
        if missing or not chosen:
            return ToolResult.fail(f"Not attached by you: {', '.join(missing) or names}. Attached: "
                                   f"{', '.join(available)}.", "not_found")
        folder = Path(str(arguments.get("folder") or downloads).strip()).expanduser()
        if not folder.is_absolute():
            folder = Path.home() / folder
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for file in chosen:
            target = free_name(folder, Path(file.name or Path(file.path).name).name)
            shutil.copy2(file.path, target)
            saved.append(str(target))
        return ToolResult.ok("Saved " + ", ".join(saved) + ".", data={"paths": saved})

    async def read_clipboard(arguments, tool) -> ToolResult:
        try:
            text = await desktop.clipboard()
        except DesktopError as error:
            return failed(error)
        return ToolResult.ok(text[:4000] + ("…" if len(text) > 4000 else "") if text.strip() else "(the clipboard "
                             "has no text)")

    async def write_clipboard(arguments, tool) -> ToolResult:
        try:
            await desktop.clipboard(str(arguments["text"]))
        except DesktopError as error:
            return failed(error)
        return ToolResult.ok("Copied to the computer's clipboard.")

    async def open_target(arguments, tool) -> ToolResult:
        target = str(arguments["target"]).strip()
        if not URL.match(target):
            path = Path(target).expanduser()
            if not path.is_absolute():
                path = Path.home() / path
            if not path.exists():
                return ToolResult.fail(f"{target} is neither an http(s) URL nor an existing file or folder",
                                       "not_found")
            target = str(path)
        try:
            await desktop.open(target)
        except DesktopError as error:
            return failed(error)
        return ToolResult.ok(f"Opened {target} on the computer.")

    async def processes(arguments, tool) -> ToolResult:
        def sample():
            items = list(psutil.process_iter(["pid", "name", "memory_info"]))
            for item in items:
                try:
                    item.cpu_percent(None)
                except psutil.Error:
                    pass
            time.sleep(0.5)
            rows = []
            for item in items:
                try:
                    rows.append((item.cpu_percent(None), item.info["memory_info"].rss if item.info["memory_info"]
                                 else 0, item.info["pid"], item.info["name"] or "?"))
                except psutil.Error:
                    continue
            return rows
        rows = await asyncio.to_thread(sample)
        key = 1 if str(arguments.get("sort") or "cpu") == "memory" else 0
        rows.sort(key=lambda row: row[key], reverse=True)
        count = max(1, min(int(arguments.get("count") or 10), 30))
        lines = [f"{name[:28]:<28} pid {pid:<7} cpu {cpu:5.1f}%  mem {rss / 1048576:7.0f} MB"
                 for cpu, rss, pid, name in rows[:count]]
        text = "\n".join(lines)
        return ToolResult.ok(f"```\n{text}\n```" if tool.display.markdown else text)

    where = f"the bot's own computer ({SYSTEM})"
    return Contributions(capabilities=[
        Capability("host.screenshot", f"Take a screenshot of {where} and send it here.", screenshot,
                   risk=Risk.READ, access="creator", needs={"files"}, private=True,
                   command=Command("screenshot", "Screenshot of this computer's screen (creator only)",
                                   access="creator"), timeout_s=60),
        Capability("host.get_file", f"Send a file from {where} into this conversation. path: absolute, or "
                   "relative to the home folder.", get_file, {"path": {"type": "string"}}, ("path",),
                   Risk.READ, access="creator", needs={"files"}, command="getfile", command_argument="path",
                   timeout_s=120, private=True, subject="path", subject_kind="path"),
        Capability("host.save_files", f"Save files the owner attached to their own messages onto {where}. "
                   "names: the attachments' file names to save (all of the owner's if omitted); folder: where "
                   f"to (default {downloads}); names that exist get a number.", save_files,
                   {"names": {"type": "array", "items": {"type": "string"}}, "folder": {"type": "string"}},
                   risk=Risk.CRITICAL, access="creator", subject="folder", subject_kind="path",
                   describe=lambda a: f"Save {', '.join(a.get('names') or []) or 'all attached files'} into "
                                      f"{a.get('folder') or downloads}",
                   command=Command("savefile", "Save the attached file(s) onto this computer (creator only)",
                                   (Option("file", "The file to save", "attachment", True),
                                    Option("folder", f"Where to (default {downloads})")),
                                   template="[{folder}]", access="creator"), timeout_s=120),
        Capability("host.read_clipboard", f"The text on {where}'s clipboard.", read_clipboard, risk=Risk.READ,
                   access="creator", private=True, command=Command("clipboard", "Show this computer's clipboard (creator only)",
                                                     access="creator"), timeout_s=30),
        Capability("host.write_clipboard", f"Put text on {where}'s clipboard.", write_clipboard,
                   {"text": {"type": "string"}}, ("text",), Risk.WRITE, access="creator", command="copy",
                   command_argument="text", timeout_s=30),
        Capability("host.open", f"Open an http(s) URL in the browser, or a file or folder with its default app, "
                   f"on {where}'s screen.", open_target, {"target": {"type": "string"}}, ("target",), Risk.WRITE,
                   access="creator", command="open", command_argument="target", timeout_s=30),
        Capability("host.processes", f"What is running on {where}: the busiest processes. sort: cpu or memory.",
                   processes, {"sort": {"type": "string", "enum": ["cpu", "memory"]},
                               "count": {"type": "integer"}}, risk=Risk.READ, access="creator", private=True,
                   command="processes", command_argument="sort", timeout_s=30),
    ])


EXTENSION = Extension("host", setup, contracts="1", version="1",
                      description="This computer's screen, files, clipboard and apps, for the creator", settings=(
    Setting("downloads_dir", "Where $savefile puts attachments", "path", "~/Downloads"),
))

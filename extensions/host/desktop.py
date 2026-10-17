"""The computer's desktop on Windows, macOS and Linux: screenshots, clipboard, opening things, files.

Each action tries the platform's usual tools in order and says plainly which are missing. On Linux a
bot started as a service has no DISPLAY / WAYLAND_DISPLAY, so the logged-in user's session is found
(`/run/user/<uid>/wayland-0`, display `:0`); `desktop_env` in the module settings overrides it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Sequence

WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"


class DesktopError(RuntimeError):
    pass


def session_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    if not WINDOWS and not MAC:
        runtime = environment.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        environment.setdefault("XDG_RUNTIME_DIR", runtime)
        if not environment.get("WAYLAND_DISPLAY") and Path(runtime, "wayland-0").exists():
            environment["WAYLAND_DISPLAY"] = "wayland-0"
        if not environment.get("DISPLAY"):
            environment["DISPLAY"] = ":0"
        bus = Path(runtime, "bus")
        if not environment.get("DBUS_SESSION_BUS_ADDRESS") and bus.exists():
            environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    environment.update({str(key): str(value) for key, value in (overrides or {}).items()})
    return environment


def free_name(folder: Path, name: str) -> Path:
    """`name` in `folder`, or `name (1)`, `name (2)`… if taken."""
    path = folder / name
    stem, suffix, number = path.stem, path.suffix, 1
    while path.exists():
        path = folder / f"{stem} ({number}){suffix}"
        number += 1
    return path


class Desktop:
    def __init__(self, environment: Mapping[str, str] | None = None):
        self.environment = session_env(environment)

    async def run(self, argv: Sequence[str], data: bytes | None = None, timeout: float = 15.0,
                  capture: bool = True) -> bytes:
        """Run a desktop tool and return its output; DesktopError (with its message) if it fails.
        `capture=False` for tools that leave a background child serving the clipboard (wl-copy, xclip):
        it would keep captured pipes open, so only the exit of the tool itself is awaited."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL, env=self.environment)
        except OSError as error:
            raise DesktopError(f"{Path(argv[0]).name} could not run: {error}")
        try:
            if capture:
                output, error = await asyncio.wait_for(process.communicate(data), timeout)
            else:
                if data is not None:
                    process.stdin.write(data)
                    await process.stdin.drain()
                    process.stdin.close()
                await asyncio.wait_for(process.wait(), timeout)
                output, error = b"", b""
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise DesktopError(f"{Path(argv[0]).name} did not finish within {timeout:.0f} s")
        except (BrokenPipeError, ConnectionResetError):
            await process.wait()
            output, error = b"", b""
        if process.returncode != 0:
            detail = error.decode("utf-8", "replace").strip().splitlines()
            raise DesktopError(f"{Path(argv[0]).name} failed: {detail[-1] if detail else f'code {process.returncode}'}")
        return output

    def wayland(self) -> bool:
        return bool(self.environment.get("WAYLAND_DISPLAY"))

    @staticmethod
    def available(tool: str) -> bool:
        return shutil.which(tool) is not None

    # ---- screenshot -------------------------------------------------------------------------------
    async def screenshot(self, path: Path) -> Path:
        if WINDOWS or MAC:
            return await self._grab(path)
        failures = []
        if self.wayland():
            # X11 tools only see XWayland (a black frame) under Wayland, so use Wayland ones: grim on
            # wlroots compositors (sway, labwc, wayfire), the desktop's own tool on GNOME and KDE.
            candidates = [["grim", str(path)], ["gnome-screenshot", "-f", str(path)],
                          ["spectacle", "-b", "-n", "-f", "-o", str(path)]]
        else:
            candidates = [["scrot", "--overwrite", str(path)]]
        for argv in candidates:
            if not self.available(argv[0]):
                continue
            try:
                await self.run(argv, timeout=20)
                if path.is_file() and path.stat().st_size:
                    return path
            except DesktopError as error:
                failures.append(str(error))
        if self.wayland():
            raise DesktopError("No Wayland screenshot tool worked (grim for wlroots desktops such as the "
                               "Raspberry Pi's, gnome-screenshot for GNOME, spectacle for KDE). "
                               + " | ".join(failures))
        try:
            return await self._grab(path)
        except DesktopError as error:
            failures.append(str(error))
        raise DesktopError("No screenshot was possible (X11: install scrot). " + " | ".join(failures))

    async def _grab(self, path: Path) -> Path:
        def grab():
            from PIL import ImageGrab
            if WINDOWS:
                image = ImageGrab.grab(all_screens=True)
            elif MAC:
                image = ImageGrab.grab()
            else:
                image = ImageGrab.grab(xdisplay=self.environment.get("DISPLAY"))  # X11 only, never under Wayland
            image.save(path)
        try:
            await asyncio.to_thread(grab)
        except Exception as error:
            raise DesktopError(f"Screen capture failed: {type(error).__name__}: {error}")
        return path

    # ---- clipboard --------------------------------------------------------------------------------
    def clipboard_tools(self, write: bool) -> list[list[str]]:
        if WINDOWS:
            # Both directions in UTF-8: Windows PowerShell's console otherwise uses the OEM code page.
            command = ("[Console]::InputEncoding = [Text.UTF8Encoding]::new($false); "
                       "Set-Clipboard -Value ([Console]::In.ReadToEnd())" if write
                       else "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); Get-Clipboard -Raw")
            return [["powershell", "-NoProfile", "-NonInteractive", "-Command", command]]
        if MAC:
            return [["pbcopy"]] if write else [["pbpaste"]]
        tools = []
        if self.wayland():
            tools.append(["wl-copy"] if write else ["wl-paste", "--no-newline"])
        tools += ([["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]] if write
                  else [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]])
        return tools

    async def clipboard(self, text: str | None = None) -> str:
        """The clipboard's text, or (with `text`) replace it and return ``""``."""
        failures = []
        write = text is not None
        for argv in self.clipboard_tools(write):
            if not self.available(argv[0]):
                continue
            try:
                # Writers other than PowerShell leave a child serving the clipboard; don't wait on its pipes.
                output = await self.run(argv, text.encode("utf-8") if write else None, timeout=10,
                                        capture=not write or WINDOWS)
                return "" if write else output.decode("utf-8", "replace")
            except DesktopError as error:
                failures.append(str(error))
        raise DesktopError("No clipboard tool worked (Linux: install wl-clipboard for Wayland, or xclip/xsel). "
                           + " | ".join(failures))

    # ---- opening ----------------------------------------------------------------------------------
    async def open(self, target: str) -> None:
        """Open a URL or a file with the desktop's default app, without waiting for it."""
        if WINDOWS:
            await asyncio.to_thread(os.startfile, target)  # type: ignore[attr-defined]
            return
        tool = "open" if MAC else "xdg-open"
        if not self.available(tool):
            raise DesktopError(f"{tool} is not installed")
        try:
            subprocess.Popen([tool, target], env=self.environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            raise DesktopError(f"{tool} could not run: {error}")

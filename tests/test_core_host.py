"""Host module: the computer's screen, files, clipboard and apps, creator-only, writes approved."""

import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from contracts import Display, FileRef
from core.loader import import_extension
from tests.core_support import FIXTURES, ROOT, iface, settle, start_core


def host_modules():
    extension, module = import_extension(ROOT / "extensions" / "host")
    package = module.__name__.rsplit(".", 1)[0]
    return extension, module, __import__(package + ".desktop", fromlist=["Desktop"])


class FakeDesktop:
    def __init__(self):
        self.clip, self.opened = "", []

    async def screenshot(self, path):
        from PIL import Image
        Image.new("RGB", (800, 600), (40, 90, 160)).save(path)
        return path

    async def clipboard(self, text=None):
        if text is None:
            return self.clip
        self.clip = text
        return ""

    async def open(self, target):
        self.opened.append(target)


class CapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        extension, self.module, self.desktop_module = host_modules()
        self.fake = FakeDesktop()
        self.module.Desktop = lambda environment: self.fake
        context = SimpleNamespace(config={"downloads_dir": str(self.tmp / "downloads")}, data_dir=self.tmp,
                                  log=lambda *a, **k: None)
        self.capabilities = {c.name: c for c in extension.setup(context).capabilities}

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def tool(self, files=(), upload_mb=8.0, others=()):
        workspace = self.tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        return SimpleNamespace(workspace=workspace, files=tuple(others) + tuple(files), own_files=tuple(files),
                               display=Display(markdown=True, max_upload_mb=upload_mb))

    async def call(self, name, arguments=None, **tool):
        return await self.capabilities[name].run(arguments or {}, self.tool(**tool))

    def test_only_the_process_list_is_a_read_and_everything_is_creator_only(self):
        self.assertEqual({c.name for c in self.capabilities.values() if c.risk.value != "write"}, {"host.processes"})
        self.assertEqual({c.access for c in self.capabilities.values()}, {"creator"})

    async def test_screenshots_are_sent_and_shrunk_to_fit(self):
        result = await self.call("host.screenshot")
        self.assertEqual((result.status, Path(result.files[0].path).suffix), ("success", ".png"))
        result = await self.call("host.screenshot", upload_mb=0.002)
        self.assertEqual((result.status, Path(result.files[0].path).suffix), ("success", ".jpg"))
        self.assertLessEqual(Path(result.files[0].path).stat().st_size, 0.002 * 1024 * 1024)

    async def test_files_go_out_as_snapshots_and_come_in_without_overwriting(self):
        original = self.tmp / "notes.txt"
        original.write_text("v1")
        result = await self.call("host.get_file", {"path": str(original)})
        original.write_text("v2")
        self.assertEqual(Path(result.files[0].path).read_text(), "v1")
        self.assertEqual((await self.call("host.get_file", {"path": str(self.tmp)})).error_code, "not_found")
        self.assertEqual((await self.call("host.get_file", {"path": str(original)}, upload_mb=0.000001)).error_code,
                         "too_large")
        incoming = FileRef(str(original), "notes.txt")
        first = await self.call("host.save_files", files=[incoming])
        second = await self.call("host.save_files", files=[incoming])
        self.assertEqual([Path(path).name for path in first.data["paths"] + second.data["paths"]],
                         ["notes.txt", "notes (1).txt"])
        self.assertEqual((await self.call("host.save_files")).error_code, "no_files")
        elsewhere = await self.call("host.save_files", {"folder": str(self.tmp / "in")}, files=[incoming])
        self.assertTrue(Path(elsewhere.data["paths"][0]).is_file())

    async def test_only_the_creators_own_named_attachments_are_saved(self):
        mine, theirs = self.tmp / "mine.txt", self.tmp / "evil.desktop"
        mine.write_text("mine")
        theirs.write_text("[Desktop Entry]")
        own, other = FileRef(str(mine), "mine.txt"), FileRef(str(theirs), "evil.desktop")
        folder = {"folder": str(self.tmp / "saved")}
        result = await self.call("host.save_files", folder, files=[own], others=[other])
        self.assertEqual([Path(path).name for path in result.data["paths"]], ["mine.txt"])
        refused = await self.call("host.save_files", {**folder, "names": ["evil.desktop"]}, files=[own],
                                  others=[other])
        self.assertEqual(refused.error_code, "not_found")
        self.assertEqual((await self.call("host.save_files", others=[other])).error_code, "no_files")
        self.assertFalse((self.tmp / "saved" / "evil.desktop").exists())

    async def test_clipboard_open_and_processes(self):
        await self.call("host.write_clipboard", {"text": "hi"})
        self.assertEqual((await self.call("host.read_clipboard")).summary, "hi")
        await self.call("host.open", {"target": "https://example.com"})
        await self.call("host.open", {"target": str(self.tmp)})
        self.assertEqual(self.fake.opened, ["https://example.com", str(self.tmp)])
        self.assertEqual((await self.call("host.open", {"target": "javascript:alert(1)"})).error_code, "not_found")
        listing = await self.call("host.processes", {"sort": "memory", "count": 3})
        self.assertEqual(listing.summary.count(" pid "), 3)

    def test_a_service_finds_the_desktop_session(self):
        environment = self.desktop_module.session_env({"DISPLAY": ":5"})
        self.assertEqual(environment["DISPLAY"], ":5")
        if self.desktop_module.WINDOWS or self.desktop_module.MAC:
            return
        self.assertIn("XDG_RUNTIME_DIR", environment)


@unittest.skipIf(__import__("os").name == "nt", "stub tools are POSIX shell scripts")
class DesktopTests(unittest.IsolatedAsyncioTestCase):
    """The real Desktop code, against stub tools on PATH."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        _, _, self.module = host_modules()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def desktop(self, **environment):
        from unittest import mock
        for tool in ("cat", "sleep", "sh"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        patcher = mock.patch.dict("os.environ", {"PATH": str(self.bin)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.module.Desktop({"XDG_RUNTIME_DIR": str(self.tmp), **environment})

    async def test_a_clipboard_writer_that_stays_in_the_background_does_not_hang(self):
        # Like wl-copy and xclip: the tool exits, a child keeps serving the clipboard with stdout/stderr open.
        self.stub("wl-copy", f'cat > "{self.tmp}/clip"; (sleep 30) & exit 0\n')
        self.stub("wl-paste", f'cat "{self.tmp}/clip"\n')
        desktop = self.desktop(WAYLAND_DISPLAY="wayland-0")
        started = time.monotonic()
        await desktop.clipboard("héllo ✓")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(await desktop.clipboard(), "héllo ✓")

    async def test_wayland_screenshots_never_fall_back_to_x11(self):
        self.stub("scrot", f'echo x11 > "$2"\n')
        self.stub("grim", "echo 'grim: compositor does not support wlr-screencopy' >&2; exit 1\n")
        desktop = self.desktop(WAYLAND_DISPLAY="wayland-0", DISPLAY=":0")
        with self.assertRaisesRegex(self.module.DesktopError, "No Wayland screenshot tool worked"):
            await desktop.screenshot(self.tmp / "shot.png")
        self.stub("gnome-screenshot", 'echo png > "$2"\n')
        self.assertEqual((await desktop.screenshot(self.tmp / "shot.png")).read_text(), "png\n")
        x11 = self.module.Desktop({"XDG_RUNTIME_DIR": str(self.bin), "WAYLAND_DISPLAY": "", "DISPLAY": ":0"})
        self.assertEqual((await x11.screenshot(self.tmp / "x.png")).read_text(), "x11\n")


class HostPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.core = await start_core(Path(self._tmp.name), {"extensions": [
            str(FIXTURES / "fake"), str(FIXTURES / "probe"), str(ROOT / "extensions" / "host")]})
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def test_models_need_the_creator_and_their_approval_but_the_creators_command_is_it(self):
        await self.chat.say('tool host.open {"target": "https://example.com"}', sender="boss")
        await self.chat.say('tool host.open {"target": "https://example.com"}', conversation="guest")
        await self.chat.say('tool host.processes {"count": 1}', sender="boss", conversation="mine")
        await settle(self.core)
        approvals = self.core.tank.approvals("pending")
        self.assertEqual([(item["name"], item["arguments"]) for item in approvals],
                         [("host.open", {"target": "https://example.com"})])
        self.assertEqual(self.chat.texts("guest", "text"), ["failed: host.open is available only to the owner"])
        self.assertRegex(self.chat.texts("mine", "text")[0], r"^success: .+ pid \d+ +cpu ")
        await self.chat.say("$processes memory", conversation="guest")
        await settle(self.core)
        self.assertEqual(self.chat.texts("guest", "text")[-1], "host.processes is available only to the owner")
        hidden = {command.name: command.access for command in self.core.manager.commands()}
        self.assertEqual({hidden[name] for name in ("screenshot", "getfile", "savefile", "clipboard", "copy", "open",
                                                    "processes")}, {"creator"})

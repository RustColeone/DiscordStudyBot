"""The web UI plugin: a chat page on the local network, run against a real core."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp

from contracts import Address, Reply, Request, Sender
from tests.core_support import ROOT, iface, make_config, settle, start_core

PLUGIN = ROOT / "plugins" / "webui"


class HelperTests(unittest.TestCase):
    def test_only_local_peers_and_local_host_names(self):
        from ipaddress import ip_network
        from plugins_webui import local_host, local_remote
        for remote, expected in (("127.0.0.1", True), ("10.0.0.71", True), ("192.168.1.20", True),
                                 ("::1", True), ("::ffff:192.168.1.2", True), ("fe80::1%eth0", True),
                                 ("8.8.8.8", False), ("100.64.1.1", False), ("", False), ("bogus", False)):
            self.assertEqual(local_remote(remote, []), expected, remote)
        self.assertTrue(local_remote("100.64.1.1", [ip_network("100.64.0.0/10")]))
        self.assertFalse(local_remote("10.0.0.71", [ip_network("100.64.0.0/10")]))
        for host, expected in (("10.0.0.71:4156", True), ("localhost:4156", True), ("[::1]:4156", True),
                               ("raspberrypi.local:4156", True), ("pi.lan", True), ("evil.example:4156", False),
                               ("10.0.0.71.nip.io", False), ("", False)):
            self.assertEqual(local_host(host, set()), expected, host)
        self.assertTrue(local_host("studybot.home:4156", {"studybot.home"}))


def load_helpers():
    """Import the plugin's interface module the way the core does, for the helper tests."""
    import sys
    from core.loader import import_extension
    _, module = import_extension(PLUGIN)
    sys.modules["plugins_webui"] = sys.modules[module.__name__.rsplit(".", 1)[0] + ".interface"]


load_helpers()


class WebUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None
        # Like a browser on the LAN, keep cookies set by an IP-address host (aiohttp drops them by default).
        self.http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))

    async def asyncTearDown(self):
        await self.http.close()
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, settings=None):
        paths = [*make_config(self.tmp).raw["modules"]["paths"], str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": paths,
            "extension_config": {"webui": {"host": "127.0.0.1", "port": 0, "user_name": "Leo", **(settings or {})}},
            "assignments": {"webui:*": {"engine": "probe", "personas": ["p"]}}})
        self.web = iface(self.core, "webui")
        self.base = f"http://127.0.0.1:{self.web.port_number}"
        return self.web

    async def socket(self, **kwargs):
        ws = await self.http.ws_connect(f"{self.base}/ws", **kwargs)
        hello = await ws.receive_json(timeout=5)
        self.assertEqual(hello["type"], "hello")
        return ws, hello["state"]

    async def until(self, ws, predicate, limit=20):
        seen = []
        for _ in range(limit):
            try:
                event = await ws.receive_json(timeout=5)
            except asyncio.TimeoutError:
                break
            seen.append(event)
            if predicate(event):
                return event, seen
        self.fail(f"never arrived; saw {[(e['type'], e.get('item', {}).get('kind')) for e in seen]}")

    async def say(self, ws, text, conversation="classroom", **extra):
        await ws.send_json({"type": "send", "conversation": conversation, "text": text, "id": extra.pop("id", None)
                            or f"t{id(text)}{len(text)}", **extra})

    # ---- talking --------------------------------------------------------------------------------
    async def test_a_message_reaches_the_engine_as_the_creator_and_the_answer_arrives_live_and_stays(self):
        await self.start()
        ws, state = await self.socket()
        self.assertEqual((state["user"]["name"], state["default"]), ("Leo", "classroom"))
        self.assertEqual(state["classmates"], ["Luna", "Gemini", "Claude", "DeepSeek"])
        self.assertIn("help", [command["name"] for command in state["commands"]])
        await self.say(ws, "echo hello class")
        answer, seen = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text")
        self.assertEqual(answer["item"]["text"], "hello class")
        mine = [e["item"] for e in seen if e["type"] == "message" and e["item"]["kind"] == "user"]
        self.assertEqual((mine[0]["text"], mine[0]["sender"]), ("echo hello class", "Leo"))
        ack, _ = await self.until(ws, lambda e: e["type"] == "ack")
        self.assertTrue(ack["ok"])
        self.assertTrue(self.core.manager.is_owner(Request("x", Address("webui", "classroom"), Sender("creator"), "", 0)))
        async with self.http.get(f"{self.base}/api/history?conversation=classroom") as response:
            history = (await response.json())["messages"]
        self.assertEqual([item["kind"] for item in history], ["user", "text"])
        await ws.close()

    async def test_new_chats_are_their_own_conversations_and_take_their_first_line_as_title(self):
        await self.start()
        ws, _ = await self.socket()
        await ws.send_json({"type": "new", "conversation": "chat-abc", "title": "New chat"})
        await self.until(ws, lambda e: e["type"] == "conversations")
        await self.say(ws, "echo in a new chat\nsecond line", conversation="chat-abc")
        answer, _ = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text")
        self.assertEqual(answer["item"]["conversation"], "chat-abc")
        titles = {chat["id"]: chat["title"] for chat in self.web.store.conversations()}
        self.assertEqual(titles["chat-abc"], "echo in a new chat")
        self.assertTrue(self.core.tank.recent_messages("webui:chat-abc", 5))
        await ws.send_json({"type": "send", "conversation": "../etc", "text": "echo nope", "id": "bad"})
        await ws.send_json({"type": "rename", "conversation": "chat-abc", "title": "Physics"})
        await self.until(ws, lambda e: e["type"] == "conversations"
                         and any(chat["title"] == "Physics" for chat in e["items"]))
        self.assertEqual(self.web.store.conversation("chat-abc")["title"], "Physics")
        self.assertIsNone(self.web.store.conversation("../etc"))
        await ws.close()

    async def test_chats_can_be_pinned_reordered_and_deleted_with_their_files(self):
        await self.start()
        ws, _ = await self.socket()
        for chat in ("chat-a", "chat-b"):
            await ws.send_json({"type": "new", "conversation": chat, "title": chat})
            await self.until(ws, lambda e: e["type"] == "conversations")
        order = lambda: [chat["id"] for chat in self.web.store.conversations()]
        self.assertEqual(order(), ["chat-b", "chat-a", "classroom"])  # new chats on top
        await ws.send_json({"type": "order", "conversations": ["classroom", "chat-a", "chat-b", "../x"]})
        await self.until(ws, lambda e: e["type"] == "conversations")
        self.assertEqual(order(), ["classroom", "chat-a", "chat-b"])
        await ws.send_json({"type": "pin", "conversation": "chat-b", "pinned": True})
        await self.until(ws, lambda e: e["type"] == "conversations")
        self.assertEqual(order(), ["chat-b", "classroom", "chat-a"])
        await self.say(ws, 'tool tools.make_file {"name": "notes.txt"}', conversation="chat-a", id="d1")
        delivered, _ = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "file")
        stored = Path(self.web.store.file(delivered["item"]["file"]["id"])["path"])
        self.assertTrue(stored.is_file())
        await ws.send_json({"type": "delete", "conversation": "chat-a"})
        deleted, _ = await self.until(ws, lambda e: e["type"] == "deleted")
        self.assertEqual((deleted["conversation"], [chat["id"] for chat in deleted["items"]]),
                         ("chat-a", ["chat-b", "classroom"]))
        self.assertEqual(self.web.store.messages("chat-a"), [])
        self.assertFalse(stored.exists())
        self.assertIsNone(self.web.store.file(delivered["item"]["file"]["id"]))
        for chat in ("chat-b", "classroom"):
            await ws.send_json({"type": "delete", "conversation": chat})
            await self.until(ws, lambda e: e["type"] == "deleted")
        self.assertEqual(order(), ["classroom"])  # there is always one chat to land in
        await ws.close()

    async def test_approvals_arrive_as_cards_and_a_button_press_decides_them(self):
        await self.start()
        ws, _ = await self.socket()
        await self.say(ws, 'tool tools.write_value {"key": "k", "value": "v"}')
        card, _ = await self.until(ws, lambda e: e["type"] == "message" and e["item"].get("code") == "approval_request")
        item = card["item"]
        self.assertEqual([action["label"] for action in item["actions"]], ["Approve", "Deny"])
        self.assertEqual(item["data"]["tier"], "write")
        approve = item["actions"][0]["command"]
        await self.say(ws, approve, action="Approve", id="press1")
        _, seen = await self.until(ws, lambda e: e["type"] == "ack" and e["id"] == "press1")
        update = next(e for e in seen if e["type"] == "update")
        self.assertEqual((update["item"]["id"], update["item"]["decided"]), (item["id"], "approved"))
        pressed = next(e["item"] for e in seen if e["type"] == "message" and e["item"]["kind"] == "user")
        self.assertEqual(pressed["action"], "Approve")
        answers = [e["item"]["text"] for e in seen if e["type"] == "message" and e["item"]["kind"] == "text"]
        self.assertTrue(any(text.startswith("approved: wrote k") for text in answers), answers)
        await ws.close()

    async def test_typing_lines_follow_a_run_until_it_says_something(self):
        web = await self.start()
        ws, _ = await self.socket()
        await web.deliver(Reply(Address("webui", "classroom"), "progress", text="Luna is responding…", run_id="r1"))
        event = await ws.receive_json(timeout=5)
        self.assertEqual((event["type"], event["run_id"], event["text"]), ("progress", "r1", "Luna is responding…"))
        ws2, state = await self.socket()
        self.assertEqual([item["run_id"] for item in state["progress"]], ["r1"])
        await web.deliver(Reply(Address("webui", "classroom"), "text", text="Hi!", run_id="r1", speaker="Luna"))
        cleared, seen = await self.until(ws, lambda e: e["type"] == "progress_clear")
        message, _ = await self.until(ws, lambda e: e["type"] == "message")
        self.assertEqual((message["item"]["speaker"], message["item"]["text"]), ("Luna", "Hi!"))
        self.assertEqual(web.state()["progress"], [])
        await ws.close()
        await ws2.close()

    # ---- files ----------------------------------------------------------------------------------
    async def test_files_go_both_ways_and_only_pictures_and_media_open_in_the_page(self):
        await self.start()
        ws, _ = await self.socket()
        form = aiohttp.FormData()
        form.add_field("file", b"\x89PNG\r\n\x1a\nfake", filename="photo.png", content_type="image/png")
        async with self.http.post(f"{self.base}/api/upload", data=form) as response:
            uploaded = (await response.json())["files"][0]
        self.assertEqual((uploaded["name"], uploaded["mime"]), ("photo.png", "image/png"))
        await ws.send_json({"type": "send", "conversation": "classroom", "text": "echo look", "id": "f1",
                            "files": [uploaded["id"]]})
        await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text")
        await settle(self.core)
        run = self.core.tank.runs(("done",), address="webui:classroom")[0]
        files = self.core.tank.events(run.id, ("input",))[0].data["request"]["files"]
        self.assertEqual(files[0]["name"], "photo.png")
        async with self.http.get(f"{self.base}/files/{uploaded['id']}") as response:
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
        await self.say(ws, 'tool tools.make_file {"name": "page.html"}', id="f2")
        delivered, _ = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "file")
        async with self.http.get(f"{self.base}/files/{delivered['item']['file']['id']}") as response:
            self.assertEqual(await response.text(), "payload")
            self.assertEqual(response.headers["Content-Type"], "application/octet-stream")
            self.assertTrue(response.headers["Content-Disposition"].startswith("attachment"))
        async with self.http.get(f"{self.base}/files/nope") as response:
            self.assertEqual(response.status, 404)
        await ws.close()

    async def test_avatars_are_listed_and_served_and_nothing_else_from_the_folders(self):
        web = await self.start()
        folder = self.tmp / "plugin"
        (folder / "avatars").mkdir(parents=True)
        (folder / "static").symlink_to(PLUGIN / "static")
        (folder / "avatars" / "Luna.PNG").write_bytes(b"\x89PNG\r\n\x1a\nluna")
        (folder / "avatars" / "notes.txt").write_text("not a picture")
        web.folder = folder
        ws, state = await self.socket()
        self.assertEqual(state["avatars"], ["luna"])
        async with self.http.get(f"{self.base}/avatars/luna") as response:
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
        for path in ("/avatars/notes", "/avatars/..%2Fstatic%2Fapp", "/static/..%2Finterface.py", "/static/login.html"):
            async with self.http.get(self.base + path) as response:
                self.assertEqual(response.status, 404, path)
        await ws.close()

    # ---- guards ---------------------------------------------------------------------------------
    async def test_other_sites_and_rebound_names_are_refused_and_pages_carry_a_strict_policy(self):
        await self.start()
        async with self.http.get(f"{self.base}/") as response:
            self.assertEqual(response.status, 200)
            policy = response.headers["Content-Security-Policy"]
            self.assertIn("script-src 'self' https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js", policy)
            self.assertIn("frame-ancestors 'none'", policy)
        async with self.http.get(f"{self.base}/", headers={"Host": "evil.example"}) as response:
            self.assertEqual(response.status, 403)
        async with self.http.get(f"{self.base}/api/history", headers={"Origin": "http://evil.example"}) as response:
            self.assertEqual(response.status, 403)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as refused:
            await self.http.ws_connect(f"{self.base}/ws", headers={"Origin": "http://evil.example"})
        self.assertEqual(refused.exception.status, 403)
        ws, _ = await self.socket(headers={"Origin": self.base})
        await ws.close()

    async def test_a_passphrase_is_asked_once_per_browser_when_set(self):
        await self.start({"passphrase": "open sesame"})
        async with self.http.get(f"{self.base}/", allow_redirects=False) as response:
            self.assertEqual((response.status, response.headers["Location"]), (303, "/login"))
        for path in ("/api/history", "/static/app.js"):
            async with self.http.get(self.base + path) as response:
                self.assertEqual(response.status, 401, path)
        async with self.http.get(f"{self.base}/favicon.ico") as response:  # the sign-in page shows it too
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
        with self.assertRaises(aiohttp.WSServerHandshakeError):
            await self.http.ws_connect(f"{self.base}/ws")
        async with self.http.post(f"{self.base}/login", data={"passphrase": "wrong"}) as response:
            self.assertEqual(response.status, 401)
            self.assertIn("not right", await response.text())
        async with self.http.post(f"{self.base}/login", data={"passphrase": "open sesame"},
                                  allow_redirects=False) as response:
            self.assertEqual(response.status, 303)
            cookie = response.cookies["webui_session"]
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Strict")
        async with self.http.get(f"{self.base}/") as response:
            self.assertEqual(response.status, 200)
        ws, _ = await self.socket()
        await ws.close()

    async def test_the_room_is_the_default_for_its_conversations(self):
        from core.loader import import_extension
        extension, _ = import_extension(PLUGIN)

        class Context:
            name, data_dir = "webui", self.tmp
            config = {"port": 0}

            @staticmethod
            def log(*args, **kwargs):
                pass
        contributions = extension.setup(Context())
        self.assertEqual(contributions.assignments["webui:*"]["engine"], "room")
        self.assertEqual(contributions.assignments["webui:*"]["personas"], ["luna", "gemini", "claude", "deepseek"])
        self.assertFalse(contributions.admission["webui:*"]["require_mention_in_groups"])


if __name__ == "__main__":
    unittest.main()

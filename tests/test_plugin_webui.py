"""The web UI plugin: a chat page on the local network, run against a real core."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web

from contracts import Address, Reply, Request, Sender
from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

STUDENTS = ("gpt", "gemini", "claude", "deepseek", "grok")
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
        paths = [*make_config(self.tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "students"), str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": paths,
            "extension_config": {"webui": {"host": "127.0.0.1", "port": 0, "user_name": "Leo", **(settings or {})}},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in STUDENTS},
            "agents": {"q": {"display_name": "Q", "prompt": "You are Q.", "profile": "none"}},  # no model to answer
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

    # ---- the mini map -----------------------------------------------------------------------------
    async def test_the_mini_map_shows_the_places_other_interfaces_publish(self):
        lab = {"place": {"name": "Lab", "icon": "🧪", "note": "1 at work"},
               "people": [{"key": "claude", "name": "Claude", "avatar": "claude", "doing": "drafting", "title": "Fix"}],
               "away": ["claude"], "tickets": [{"key": "deepseek", "by": "DeepSeek", "title": "Add rooms",
                                                "link": "http://lab.local:4157/#t-2"}]}
        web_ = await self.start({"places_poll_s": 2})
        ws, state = await self.socket()
        self.assertIsNone(state["places"])  # nobody else published a place: no map
        seen = iface(self.core, "chat").port.directory()["webui"]
        self.assertEqual((seen["status"]["place"]["name"], seen["link"]), ("MomoChat", web_.address_shown))
        self.assertIn("deepseek", {person["key"] for person in seen["status"]["people"]})  # the classmates, published
        iface(self.core, "chat").port.publish(link="http://lab.local:4157/", status=lab)  # the lab, next door
        event, _ = await self.until(ws, lambda item: item["type"] == "places", limit=6)
        other = event["places"]["others"][0]
        self.assertEqual((other["interface"], other["tickets"][0]["title"], other["away"]), ("chat", "Add rooms", ["claude"]))
        self.assertEqual(event["places"]["here"]["place"]["name"], "MomoChat")
        ws2, state2 = await self.socket()
        self.assertEqual(state2["places"]["others"][0]["link"], "http://lab.local:4157/")  # a page opened later
        await ws.close()
        await ws2.close()

    # ---- the places bar and the cafe --------------------------------------------------------------
    async def test_the_page_publishes_its_views_for_every_places_bar(self):
        web_ = await self.start()
        ws, state = await self.socket()
        self.assertEqual(state["here"]["link"], web_.address_shown)  # the bar is drawn even with no other place
        self.assertEqual([(view["name"], view["path"], view["order"]) for view in state["here"]["views"]],
                         [("Cafe", "cafe", 10), ("MomoChat", "", 20), ("Students", "students", 40)])
        published = iface(self.core, "chat").port.directory()["webui"]["status"]
        self.assertEqual([view["name"] for view in published["views"]], ["Cafe", "MomoChat", "Students"])
        async with self.http.get(f"{self.base}/") as response:  # the page comes with them, for its first paint
            page = await response.text()
        given = json.loads(page.split('id="places-data">', 1)[1].split("</script>", 1)[0])
        self.assertEqual(([view["name"] for view in given["here"]["views"]], given["others"]), (["Cafe", "MomoChat", "Students"], []))
        await ws.close()

    async def test_the_cafe_shows_every_student_whether_she_can_answer_and_the_preset_rooms(self):
        import shutil
        from unittest.mock import patch
        web_ = await self.start({"personas": ["p", "q"], "classmates": ["Pia</script><b>", "Quinn"]})
        folder = self.tmp / "webui"  # a copy of the plugin's pages, with rooms of its own
        shutil.copytree(PLUGIN / "static", folder / "static")
        (folder / "rooms").mkdir()
        (folder / "rooms" / "garden.png").write_bytes(b"\x89PNG fake")
        (folder / "rooms" / "notes.txt").write_text("not a room")
        (folder / "secret.png").write_bytes(b"\x89PNG outside")
        web_.folder = folder
        async with self.http.get(f"{self.base}/cafe") as response:
            self.assertEqual(response.status, 200)
            page = await response.text()
        self.assertIn("<title>Cafe · ", page)
        given = json.loads(page.split('id="cafe-data">', 1)[1].split("</script>", 1)[0])  # the data comes inside
        self.assertEqual([item["display"] for item in given["students"]], ["Pia</script><b>", "Quinn"])
        self.assertNotIn("Pia</script>", page)  # a name never ends the script that carries it
        with patch.object(web_.port, "models_ready", lambda profile: profile == "p"):
            async with self.http.get(f"{self.base}/api/cafe") as response:
                data = await response.json()
        self.assertEqual([(item["name"], item["display"], item["ready"]) for item in data["students"]],
                         [("p", "Pia</script><b>", True), ("q", "Quinn", False)])  # one without a model still walks around
        self.assertEqual((data["rooms"], [view["name"] for view in data["here"]["views"]]), (["garden.png"], ["Cafe", "MomoChat", "Students"]))
        async with self.http.get(f"{self.base}/rooms/garden.png") as response:
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
        for name in ("notes.txt", "..%2Fsecret.png", "missing.png"):  # only listed pictures are served
            async with self.http.get(f"{self.base}/rooms/{name}") as response:
                self.assertEqual(response.status, 404, name)

    async def test_the_cafe_asks_for_the_passphrase_first(self):
        await self.start({"passphrase": "open sesame"})
        async with self.http.get(f"{self.base}/cafe", allow_redirects=False) as response:
            self.assertEqual((response.status, response.headers["Location"]), (303, "login"))
        async with self.http.get(f"{self.base}/api/cafe") as response:
            self.assertEqual(response.status, 401)

    # ---- talking --------------------------------------------------------------------------------
    async def test_a_message_reaches_the_engine_as_the_creator_and_the_answer_arrives_live_and_stays(self):
        await self.start()
        ws, state = await self.socket()
        self.assertEqual((state["user"]["name"], state["default"]), ("Leo", "classroom"))
        self.assertEqual(state["classmates"], ["GPT", "Gemini", "Claude", "DeepSeek", "Grok"])
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
        async with self.http.get(f"{self.base}/api/history?conversation=classroom&after=x") as response:
            self.assertEqual(response.status, 400)
        await ws.close()

    async def test_the_model_menu_asks_and_chooses_quietly_outside_the_chat(self):
        await self.start()
        ws, _ = await self.socket()
        await ws.send_json({"type": "models", "conversation": "classroom"})
        overview, seen = await self.until(ws, lambda e: e["type"] == "models")
        self.assertTrue(overview["ok"])
        self.assertEqual(overview["purpose"], "overview")
        self.assertEqual(overview["result"]["personas"][0]["model"], "fake/fake-model")
        await ws.send_json({"type": "model", "conversation": "classroom", "persona": "p", "model": "fake/fake-model"})
        chosen, more = await self.until(ws, lambda e: e["type"] == "models")
        self.assertEqual((chosen["purpose"], chosen["result"]["chosen"]), ("choose", True))
        self.assertEqual(self.core.manager.model_choices.get("webui:classroom", "p"), "fake/fake-model")
        await ws.send_json({"type": "model", "conversation": "classroom", "persona": "p", "model": "reset"})
        reset, rest = await self.until(ws, lambda e: e["type"] == "models")
        self.assertFalse(reset["result"]["chosen"])
        self.assertNotIn("message", [event["type"] for event in [*seen, *more, *rest]])  # no bubbles
        self.assertEqual(self.web.store.messages("classroom"), [])  # nor anything in the page's transcript
        await ws.send_json({"type": "model", "conversation": "classroom", "persona": "p", "model": "x; $call"})
        await ws.send_json({"type": "models", "conversation": "classroom"})
        follow, _ = await self.until(ws, lambda e: e["type"] == "models")  # the malformed one was never sent
        self.assertEqual(follow["purpose"], "overview")
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
        await web.deliver(Reply(Address("webui", "classroom"), "text", text="Hi!", run_id="r1", speaker="GPT"))
        cleared, seen = await self.until(ws, lambda e: e["type"] == "progress_clear")
        message, _ = await self.until(ws, lambda e: e["type"] == "message")
        self.assertEqual((message["item"]["speaker"], message["item"]["text"]), ("GPT", "Hi!"))
        self.assertEqual(web.state()["progress"], [])
        await ws.close()
        await ws2.close()

    # ---- the messenger page: her name on a direct chat's answers, each chat's last line, who each student is ----
    async def test_a_direct_chats_answers_are_hers_and_the_chat_list_shows_the_last_line(self):
        web = await self.start()
        web.store.ensure("dm-whale", "DeepSeek", ["deepseek"])
        dm = Address("webui", "dm-whale")
        await web.deliver(Reply(dm, "text", text="Mm-hm.  It's in the Classroom,\nwaiting for your review~",
                                data={"provider": "fake", "model": "deepseek"}))
        await web.deliver(Reply(dm, "text", text="Commands: $help, $memory"))  # a command's output: no model wrote it
        await web.deliver(Reply(dm, "notice", text="Waiting for approval…", code="waiting_approval"))
        await web.deliver(Reply(dm, "text", text="(my notes)", label="work note", speaker="DeepSeek",
                                data={"model": "deepseek"}))
        said = [(item["kind"], item["speaker"]) for item in web.store.messages("dm-whale")]
        self.assertEqual(said, [("text", "DeepSeek"), ("text", ""), ("notice", ""), ("text", "DeepSeek")])
        await web.deliver(Reply(Address("webui", "classroom"), "text", text="hi", data={"model": "gpt"}))
        self.assertEqual(web.store.messages("classroom")[-1]["speaker"], "")  # a group's answers name themselves
        chats = {chat["id"]: chat for chat in web.store.conversations()}
        # the newest thing anyone said (not a notice, not a work note), on one line
        self.assertEqual(chats["dm-whale"]["last"]["text"], "Commands: $help, $memory")
        web.store.add("dm-whale", {"kind": "user", "text": "Thanks.   Take a nap", "speaker": "", "sender": "Leo"})
        last = next(chat for chat in web.store.conversations() if chat["id"] == "dm-whale")["last"]
        self.assertEqual((last["kind"], last["text"]), ("user", "Thanks. Take a nap"))

    async def test_the_page_says_who_each_student_is_and_whether_her_own_key_is_set(self):
        await self.start()
        ws, state = await self.socket()
        people = {item["name"]: item for item in state["people"]}
        self.assertEqual(list(people), list(STUDENTS))
        self.assertIn("dragon girl", people["gpt"]["description"])
        # This core's profiles give each student one fake model and leave her tiers empty: her own tier then shows her
        # first model, and with nothing callable in it the page says her own key is missing (she answers on a
        # classmate's model, her fallback).
        self.assertEqual((people["gpt"]["own"], people["gpt"]["strong"]), (["fake/gpt"], []))
        self.assertFalse(people["gpt"]["own_ready"])
        cafe = await (await self.http.get(f"{self.base}/api/cafe")).json()
        gpt = next(item for item in cafe["students"] if item["name"] == "gpt")
        self.assertEqual((gpt["own_ready"], gpt["chats"]), (False, 1))  # the club: a chat naming nobody has everyone
        await ws.close()

    async def test_the_students_page_shows_each_students_work_from_any_modules_facts(self):
        web = await self.start()
        facts = self.core.manager.facts
        facts.write("chimera", "gpt", "record", {"level": 2, "stage": 1, "landed": 3, "discarded": 1, "open": 0})
        facts.write("chimera", "gpt", "benchmark", [{"setup": "sol", "seats": ["builder"], "points": 550}])
        async with self.http.get(f"{self.base}/students") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("<title>Students · ", await response.text())
        students = {item["name"]: item for item in (await (await self.http.get(f"{self.base}/api/students")).json())["students"]}
        gpt = students["gpt"]
        self.assertEqual((gpt["record"]["level"], gpt["benchmark"][0]["points"]), (2, 550))
        self.assertIsNone(students["claude"]["record"])  # nothing published about her yet
        self.assertIn("avatar", gpt["pictures"])
        self.assertIn("student.yml", gpt["folder"])  # her folder's files, by name
        self.assertEqual(gpt["display"], "GPT")
        async with self.http.get(f"{self.base}/pictures/gpt/avatar") as response:
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
        for path in ("/pictures/gpt/stage-9", "/pictures/nobody/avatar", "/pictures/gpt/..%2Fstudent.yml"):
            async with self.http.get(f"{self.base}{path}") as response:
                self.assertEqual(response.status, 404, path)
        ws, state = await self.socket()
        self.assertEqual(next(item for item in state["people"] if item["name"] == "gpt")["level"], 2)  # on her card too
        self.assertIn({"name": "Students", "icon": "👥", "path": "students", "order": 40}, web.place()["views"])
        await ws.close()

    async def test_her_card_renames_her_with_your_alias(self):
        web = await self.start()
        async with self.http.post(f"{self.base}/api/alias", json={"persona": "gpt", "name": "Luna  the  first"}) as response:
            answer = await response.json()
        self.assertTrue(answer["ok"], answer)
        await settle(self.core)
        self.assertEqual(next(item for item in web.standing() if item["name"] == "gpt")["alias"], "Luna the first")
        for body in ({"persona": "nobody", "name": "X"}, {"persona": "gpt", "name": "  "}, {"persona": "gpt", "name": "x" * 41}):
            async with self.http.post(f"{self.base}/api/alias", json=body) as response:
                self.assertEqual(response.status, 400, body)

    async def test_your_page_says_what_the_students_call_you_and_your_tab_follows(self):
        web = await self.start()
        self.assertEqual(web.place()["you"], {"path": "me", "name": web.user_name})  # your tab, on every page
        async with self.http.get(f"{self.base}/me") as response:
            self.assertEqual(response.status, 200)
            self.assertIn("<title>You · ", await response.text())
        async with self.http.post(f"{self.base}/api/callme", json={"name": "Captain"}) as response:
            answer = await response.json()
        self.assertTrue(answer["ok"], answer)
        self.assertEqual(answer["you"]["everyone"], "Captain")
        self.assertEqual(web.place()["you"]["name"], "Captain")
        async with self.http.post(f"{self.base}/api/callme", json={"persona": "deepseek", "name": "Master"}) as response:
            you = (await response.json())["you"]
        students = {item["name"]: item for item in you["students"]}
        self.assertEqual((students["deepseek"]["called"], students["deepseek"]["asked"]), ("Master", "Master"))
        self.assertEqual((students["gpt"]["called"], students["gpt"]["asked"]), ("Captain", None))
        for body in ({"persona": "nobody", "name": "X"}, {"name": "  "}, {"name": "x" * 41}):
            async with self.http.post(f"{self.base}/api/callme", json=body) as response:
                self.assertEqual(response.status, 400, body)
        async with self.http.post(f"{self.base}/api/link", json={}) as response:
            answer = await response.json()
        self.assertTrue(answer["ok"], answer)
        self.assertRegex(answer["code"], r"^[A-Z2-9]{6}$")  # to send from your other account
        state = await (await self.http.get(f"{self.base}/api/me")).json()
        self.assertEqual((state["account"], state["accounts"]), ("webui:creator", ["webui:creator"]))

    async def test_the_font_comes_from_the_page_itself_even_before_signing_in(self):
        await self.start({"passphrase": "open sesame"})
        async with self.http.get(f"{self.base}/static/nunito-latin.woff2") as response:
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "font/woff2"))
        async with self.http.get(f"{self.base}/static/app.js") as response:
            self.assertEqual(response.status, 401)  # everything else waits for the passphrase
        async with self.http.get(f"{self.base}/static/i18n.js") as response:
            self.assertEqual(response.status, 200)  # the sign-in page in your language
        async with self.http.get(f"{self.base}/static/prefs.js") as response:
            self.assertEqual(response.status, 200)  # and your theme
        async with self.http.get(f"{self.base}/me", allow_redirects=False) as response:
            self.assertEqual((response.status, response.headers["Location"]), (303, "login"))

    def test_the_classroom_shares_the_header_and_the_translations(self):
        for name in ("places.js", "prefs.js", "i18n.js", "i18n-zh.js"):
            self.assertEqual((PLUGIN / "static" / name).read_bytes(),
                             (ROOT / "extensions/chimera/web/static" / name).read_bytes(), name)

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
        (folder / "avatars" / "GPT.PNG").write_bytes(b"\x89PNG\r\n\x1a\nmine")  # one of yours wins over hers
        (folder / "avatars" / "notes.txt").write_text("not a picture")
        web.folder = folder
        ws, state = await self.socket()
        self.assertEqual(state["avatars"], ["claude", "deepseek", "gemini", "gpt", "grok"])  # the students' own too
        async with self.http.get(f"{self.base}/avatars/gpt") as response:
            self.assertEqual((response.status, await response.read()), (200, b"\x89PNG\r\n\x1a\nmine"))
        async with self.http.get(f"{self.base}/avatars/gemini") as response:  # from extensions/students/gemini
            self.assertEqual((response.status, response.headers["Content-Type"]), (200, "image/png"))
            self.assertEqual(await response.read(), (ROOT / "extensions" / "students" / "gemini" / "avatar.png").read_bytes())
        for path in ("/avatars/notes", "/avatars/..%2Fstatic%2Fapp", "/static/..%2Finterface.py", "/static/login.html"):
            async with self.http.get(self.base + path) as response:
                self.assertEqual(response.status, 404, path)
        await ws.close()

    async def test_the_students_names_come_from_the_core_and_a_former_name_is_hers(self):
        from plugins.webui.store import Store as WebStore
        old = WebStore(self.tmp / "data" / "extensions" / "webui" / "webui.db")  # a page from before GPT was GPT
        old.ensure("dm-luna", "Luna", ["luna"])
        old.add("dm-luna", {"kind": "text", "text": "Hi.", "speaker": "Luna", "at": 1.0})
        old.add("dm-luna", {"kind": "text", "text": "Hey.", "speaker": "Claude", "at": 2.0})
        old.close()
        web = await self.start({"personas": ["luna", "claude"]})
        self.assertEqual((web.personas, web.classmates), (["gpt", "claude"], ["GPT", "Claude"]))
        self.assertEqual(web.store.conversation("dm-luna")["personas"], ["gpt"])
        self.assertEqual([item["speaker"] for item in web.store.messages("dm-luna")], ["GPT", "Claude"])
        await self.core.stop()
        self.core = None
        web = await self.start({"personas": ["gpt"], "classmates": ["Lulu"]})  # names of the page's own win
        self.assertEqual(web.classmates, ["Lulu"])

    async def test_the_roster_says_where_each_student_stands_with_you(self):
        await self.start({"personas": ["gpt", "claude"], "places_poll_s": 2})
        facts = self.core.manager.facts.handle("students")
        facts.set("gpt", "alias", "Lulu", person=("webui", "creator"))
        facts.set("gpt", "bond", {"role": "friend", "closeness": 55}, person=("webui", "creator"))
        facts.set("gpt", "bond", {"role": "creator", "closeness": 100}, person=("chat", "creator"))  # another page's
        ws, state = await self.socket()
        gpt, claude = state["roster"]
        self.assertEqual((gpt["alias"], gpt["bond"], gpt["mood"]), ("Lulu", {"role": "friend", "closeness": 55}, None))
        self.assertEqual((claude["alias"], claude["bond"]), (None, None))
        facts.set("claude", "mood", {"value": 80, "face": "good", "feelings": []}, person=("webui", "creator"))
        event, _ = await self.until(ws, lambda e: e["type"] == "roster")
        self.assertEqual(event["roster"][1]["mood"]["face"], "good")
        await ws.close()

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
            self.assertEqual((response.status, response.headers["Location"]), (303, "login"))  # relative
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
        async with self.http.get(f"{self.base}/login") as response:
            # Not no-referrer: under it a browser sends `Origin: null` with the form below, which is refused.
            self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        async with self.http.post(f"{self.base}/login", data={"passphrase": "open sesame"},
                                  headers={"Origin": "null"}, allow_redirects=False) as response:
            self.assertEqual(response.status, 403)  # what no-referrer made browsers send
        refused = [event for event in self.core.audit.events if event.get("detail") == "refused"]
        self.assertEqual((refused[-1]["check"], refused[-1]["path"], refused[-1]["origin"]), ("origin", "/login", "null"))
        async with self.http.post(f"{self.base}/login", data={"passphrase": "open sesame"},
                                  headers={"Origin": self.base}, allow_redirects=False) as response:
            self.assertEqual(response.status, 303)  # what a browser sends under same-origin
            cookie = response.cookies["webui_session"]
            self.assertTrue(cookie["httponly"])
            self.assertEqual(cookie["samesite"], "Strict")
        async with self.http.get(f"{self.base}/") as response:
            self.assertEqual(response.status, 200)
        ws, _ = await self.socket()
        await ws.close()

    # ---- behind a reverse proxy (nginx on this machine, e.g. https://rpi.example.net/chat/) ------------
    async def proxied(self, method, path, client="8.8.8.8", proto="https", host="rpi.example.net", **kwargs):
        headers = {"Host": host, "X-Forwarded-For": client, "X-Forwarded-Proto": proto, **kwargs.pop("headers", {})}
        return await self.http.request(method, self.base + path, headers=headers, allow_redirects=False, **kwargs)

    async def test_through_the_proxy_outside_clients_need_remote_access_and_a_passphrase(self):
        await self.start({"hostnames": ["rpi.example.net"]})
        async with await self.proxied("GET", "/") as response:
            self.assertEqual(response.status, 403)  # the real client is outside the local network
        async with await self.proxied("GET", "/", client="10.0.0.5") as response:
            self.assertEqual(response.status, 200)  # a home client through the proxy is still local
        await self.core.stop()
        self.core = None
        await self.start({"hostnames": ["rpi.example.net"], "remote_access": True})
        async with await self.proxied("GET", "/") as response:
            self.assertEqual(response.status, 403)
            self.assertIn("needs a passphrase", await response.text())

    async def test_a_remote_client_signs_in_over_https_through_the_proxy(self):
        await self.start({"hostnames": ["rpi.example.net"], "remote_access": True, "passphrase": "open sesame"})
        async with await self.proxied("GET", "/") as response:
            self.assertEqual((response.status, response.headers["Location"]), (303, "login"))
        async with await self.proxied("POST", "/login", data={"passphrase": "open sesame"},
                                      headers={"Origin": "http://rpi.example.net"}) as response:
            self.assertEqual(response.status, 403)  # the page is https: an http origin is another site
        async with await self.proxied("POST", "/login", data={"passphrase": "open sesame"},
                                      headers={"Origin": "https://rpi.example.net"}) as response:
            self.assertEqual((response.status, response.headers["Location"]), (303, "./"))
            self.assertTrue(response.cookies["webui_session"]["secure"])
        refused = [event for event in self.core.audit.events if event.get("detail") == "refused"]
        self.assertEqual((refused[-1]["remote"], refused[-1]["origin"]), ("8.8.8.8", "http://rpi.example.net"))
        async with self.http.get(f"{self.base}/") as response:  # the page itself: relative URLs only
            page = await response.text()
        self.assertNotIn('"/static', page)
        self.web._remote_failures.extend([__import__("time").monotonic()] * 20)  # many addresses, many wrong tries
        async with await self.proxied("POST", "/login", client="9.9.9.9", data={"passphrase": "open sesame"},
                                      headers={"Origin": "https://rpi.example.net"}) as response:
            self.assertIn("Too many tries", await response.text())
        async with self.http.post(f"{self.base}/login", data={"passphrase": "open sesame"},
                                  allow_redirects=False) as response:
            self.assertEqual(response.status, 303)  # the home network is never locked out by that

    async def test_forwarding_headers_count_only_from_a_trusted_proxy(self):
        await self.start({"trusted_proxies": ["10.9.9.9"]})  # this test client is not the proxy
        async with await self.proxied("GET", "/", host=f"127.0.0.1:{self.web.port_number}") as response:
            self.assertEqual(response.status, 200)  # its claimed 8.8.8.8 is ignored: it is 127.0.0.1

    # ---- who is in a chat -----------------------------------------------------------------------------
    async def start_room(self):
        """The web UI with its own default (MomoChat and the five students) and a fake model for each."""
        spoke = []

        def model(messages, schema):
            import re
            speaker = re.search(r"Your name here is (\w+)", messages[0]["content"]).group(1)
            spoke.append(speaker)
            return {"status": "complete", "message": f"Hi from {speaker}", "actions": []}
        paths = [*make_config(self.tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "students"), str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": paths,
            "extension_config": {"webui": {"host": "127.0.0.1", "port": 0, "user_name": "Leo"}},
            "profiles": {name: [{"provider": "fake", "model": name}] for name in STUDENTS}},
            FakeAdapter(handler=model))
        self.web = iface(self.core, "webui")
        self.base = f"http://127.0.0.1:{self.web.port_number}"
        return spoke

    def assignment(self, conversation):
        return self.web.port.policy(Address("webui", conversation))["assignment"]

    async def test_a_new_chat_has_the_classmates_picked_for_it_and_one_makes_a_direct_chat(self):
        spoke = await self.start_room()
        ws, state = await self.socket()
        stamina = state["roster"][3].pop("stamina")  # what she spent today against her allowance (contracts 1.12)
        self.assertEqual(state["roster"][3], {"name": "deepseek", "display": "DeepSeek", "alias": None, "bond": None,
                                              "mood": None})
        self.assertEqual((stamina["today"]["spent"], stamina["today"]["allowance"], stamina["account"]), (0.0, 0.5, None))
        self.assertEqual(self.assignment("classroom")["options"],
                         {"history_limit": 40, "compact": True, "chime_in": True})
        await ws.send_json({"type": "new", "conversation": "chat-two", "title": "New chat",
                            "personas": ["claude", "gpt"]})
        await self.say(ws, "hello you two", conversation="chat-two")  # sent at once: it waits for the setup
        answer, seen = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text")
        chats = next(e for e in seen if e["type"] == "conversations")["items"]
        self.assertEqual(next(chat for chat in chats if chat["id"] == "chat-two")["personas"], ["gpt", "claude"])
        assigned = self.assignment("chat-two")
        self.assertEqual((assigned["pattern"], assigned["engine"], assigned["personas"]),
                         ("webui:chat-two", "momochat", ["gpt", "claude"]))
        self.assertEqual(assigned["options"]["chime_in"], True)  # everything else as the web UI's
        self.assertEqual(assigned["budget"]["model_calls"], 40)
        await settle(self.core)
        self.assertEqual(set(spoke), {"GPT", "Claude"})  # GPT in turn; Claude may chime in
        self.assertEqual([item["kind"] for item in self.web.store.messages("chat-two")],
                         ["user", "text", "text"])  # the quiet $call shows nowhere
        spoke.clear()
        await ws.send_json({"type": "new", "conversation": "dm-gemini", "title": "Gemini", "personas": ["gemini"]})
        await self.say(ws, "just you and me", conversation="dm-gemini")
        await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text"
                         and e["item"]["conversation"] == "dm-gemini")
        await settle(self.core)
        self.assertEqual((spoke, self.assignment("dm-gemini")["personas"]), (["Gemini"], ["gemini"]))
        self.assertEqual(self.web.store.conversation("dm-gemini")["title"], "Gemini")  # not its first line
        await ws.close()

    async def test_picking_the_whole_class_or_nobody_real_sets_no_assignment(self):
        await self.start_room()
        ws, _ = await self.socket()
        await ws.send_json({"type": "new", "conversation": "chat-all", "title": "New chat",
                            "personas": ["grok", "gpt", "gemini", "claude", "deepseek"]})
        await self.until(ws, lambda e: e["type"] == "conversations")
        self.assertEqual(self.assignment("chat-all")["pattern"], "webui:*")
        self.assertEqual(self.web.store.conversation("chat-all")["personas"],
                         ["gpt", "gemini", "claude", "deepseek", "grok"])
        for chosen in (["gpt", "root"], [], "gpt", [{"x": 1}]):
            await ws.send_json({"type": "new", "conversation": "chat-bad", "title": "x", "personas": chosen})
            error, _ = await self.until(ws, lambda e: e["type"] == "error")
            self.assertIn("Pick classmates", error["text"])
        self.assertIsNone(self.web.store.conversation("chat-bad"))
        await ws.close()

    async def test_deleting_a_chat_removes_its_own_assignment(self):
        await self.start_room()
        ws, _ = await self.socket()
        await ws.send_json({"type": "new", "conversation": "dm-gpt", "title": "GPT", "personas": ["gpt"]})
        await self.until(ws, lambda e: e["type"] == "conversations")
        for _ in range(50):
            if self.assignment("dm-gpt")["pattern"] == "webui:dm-gpt":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.assignment("dm-gpt")["personas"], ["gpt"])
        await ws.send_json({"type": "delete", "conversation": "dm-gpt"})
        await self.until(ws, lambda e: e["type"] == "deleted")
        for _ in range(50):
            if self.assignment("dm-gpt")["pattern"] == "webui:*":
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.assignment("dm-gpt")["pattern"], "webui:*")
        self.assertIsNone(self.web.store.conversation("dm-gpt"))  # the answer did not bring the chat back
        self.assertEqual(self.web.store.assigned(), [])
        # A chat the core cannot give those classmates (here: an engine for one persona) is not kept.
        self.core.manager.set_assignment("webui:*", {"engine": "probe", "personas": ["p"]})
        await ws.send_json({"type": "new", "conversation": "chat-pair", "title": "x", "personas": ["gpt", "grok"]})
        error, _ = await self.until(ws, lambda e: e["type"] == "error")
        self.assertIn("Couldn't start that chat", error["text"])
        self.assertIsNone(self.web.store.conversation("chat-pair"))
        await ws.close()

    async def test_a_message_queued_behind_a_failed_setup_is_not_sent_to_the_whole_class(self):
        spoke = await self.start_room()
        ws, _ = await self.socket()

        async def failing(conversation, personas):
            await asyncio.sleep(0.2)  # the message arrives while this runs
            return "simulated failure"
        self.web._assign = failing
        await ws.send_json({"type": "new", "conversation": "dm-gpt", "title": "GPT", "personas": ["gpt"]})
        # The same page's message is handled after the failed setup (the chat is gone by then); another
        # page's arrives while it is still running.
        other, _ = await self.socket()
        await asyncio.sleep(0.05)
        await self.say(other, "only for GPT", conversation="dm-gpt", id="meanwhile")
        await self.say(ws, "only for GPT", conversation="dm-gpt", id="queued")
        ack, _ = await self.until(other, lambda e: e["type"] == "ack")
        self.assertEqual((ack["ok"], ack["reason"]), (False, "chat_not_ready"))
        ack, seen = await self.until(ws, lambda e: e["type"] == "ack")
        self.assertEqual((ack["ok"], ack["reason"]), (False, "unknown_chat"))
        self.assertIn("simulated failure", next(e for e in seen if e["type"] == "error")["text"])
        await settle(self.core)
        self.assertEqual(spoke, [])
        self.assertIsNone(self.web.store.conversation("dm-gpt"))  # not re-created with the whole class
        self.assertFalse(self.core.tank.recent_messages("webui:dm-gpt", 5))
        await other.close()
        await ws.close()

    async def test_an_assignment_that_lands_after_its_chat_was_given_up_is_removed(self):
        import sys
        from contracts import SubmitResult
        await self.start_room()
        module = sys.modules[type(self.web).__module__]
        self.addCleanup(setattr, module, "QUIET_TIMEOUT_S", module.QUIET_TIMEOUT_S)
        module.QUIET_TIMEOUT_S = 0.2
        release, submit, held = asyncio.Event(), self.web.port.submit, []

        async def slow_core(request):  # the core takes the setup only after the page has given up waiting
            if request.text.startswith("$call core.set_assignment"):
                async def later():
                    await release.wait()
                    await submit(request)
                held.append(asyncio.create_task(later()))
                return SubmitResult(True, "accepted")
            return await submit(request)
        self.web.port.submit = slow_core
        ws, _ = await self.socket()
        await ws.send_json({"type": "new", "conversation": "dm-gpt", "title": "GPT", "personas": ["gpt"]})
        error, _ = await self.until(ws, lambda e: e["type"] == "error")
        self.assertIn("timeout", error["text"])
        self.assertIsNone(self.web.store.conversation("dm-gpt"))
        self.assertEqual(self.web.store.assigned(), ["dm-gpt"])  # it may still land
        release.set()
        await asyncio.gather(*held)
        for _ in range(60):
            if not self.web.store.assigned():
                break
            await asyncio.sleep(0.05)
        self.assertEqual((self.assignment("dm-gpt")["pattern"], self.web.store.assigned()), ("webui:*", []))
        self.assertIsNone(self.web.store.conversation("dm-gpt"))  # its answer did not bring the chat back
        await ws.close()

    async def test_at_start_only_this_pages_own_leftover_assignments_are_removed(self):
        await self.start_room()
        mine = {"engine": "momochat", "personas": ["gpt"]}
        for chat in ("dm-gone", "dm-kept", "set-by-hand"):
            self.core.manager.set_assignment(f"webui:{chat}", mine)
        self.web.store.ensure("dm-kept", "GPT", ["gpt"])
        self.web.store.mark_assigned("dm-gone")
        self.web.store.mark_assigned("dm-kept")
        await self.web._tidy()
        self.assertEqual(self.assignment("dm-gone")["pattern"], "webui:*")
        self.assertEqual(self.assignment("dm-kept")["pattern"], "webui:dm-kept")  # its chat is still there
        self.assertEqual(self.assignment("set-by-hand")["pattern"], "webui:set-by-hand")  # not ours
        self.assertEqual(self.web.store.assigned(), ["dm-kept"])
        self.assertEqual(self.web.store.messages("dm-gone"), [])  # the quiet $call showed nowhere

    async def test_answers_to_the_pages_own_commands_never_become_bubbles(self):
        await self.start()
        ws, _ = await self.socket()
        for _ in range(110):  # more model-menu asks than are kept: the oldest are dropped, and their answers too
            await ws.send_json({"type": "models", "conversation": "classroom"})
        await self.until(ws, lambda e: e["type"] == "models", limit=200)
        await settle(self.core)
        self.assertEqual(self.web.store.messages("classroom"), [])
        await ws.close()

    async def test_a_chat_deleted_while_it_is_set_up_leaves_no_assignment(self):
        await self.start_room()
        a, _ = await self.socket()
        b, _ = await self.socket()
        await a.send_json({"type": "new", "conversation": "dm-gpt", "title": "GPT", "personas": ["gpt"]})
        await b.send_json({"type": "delete", "conversation": "dm-gpt"})
        await self.until(b, lambda e: e["type"] == "deleted")
        for _ in range(60):
            if self.assignment("dm-gpt")["pattern"] == "webui:*" and not self.web.store.assigned():
                break
            await asyncio.sleep(0.05)
        self.assertEqual((self.assignment("dm-gpt")["pattern"], self.web.store.assigned()), ("webui:*", []))
        self.assertIsNone(self.web.store.conversation("dm-gpt"))
        await a.close()
        await b.close()

    async def test_at_start_a_chat_missing_its_assignment_gets_it_again(self):
        await self.start_room()
        self.web.store.ensure("dm-gpt", "GPT", ["gpt"])  # a setup cut off by a crash: the chat, no assignment
        self.assertEqual(self.assignment("dm-gpt")["pattern"], "webui:*")
        await self.web._tidy()
        self.assertEqual((self.assignment("dm-gpt")["pattern"], self.assignment("dm-gpt")["personas"]),
                         ("webui:dm-gpt", ["gpt"]))

    async def test_bad_events_and_parameters_are_refused_without_breaking_anything(self):
        await self.start({"max_upload_mb": 0.01})
        ws, _ = await self.socket()
        for event in ({"type": "order", "conversations": 5}, {"type": "order", "conversations": True},
                      {"type": "send", "conversation": "classroom", "text": "echo x", "files": 7, "id": "f7"}):
            await ws.send_json(event)
        await self.say(ws, "echo still here", id="alive")
        answer, _ = await self.until(ws, lambda e: e["type"] == "message" and e["item"]["text"] == "still here")
        self.assertFalse(ws.closed)
        for after in ("99999999999999999999", "-5"):
            async with self.http.get(f"{self.base}/api/history?conversation=classroom&after={after}") as response:
                self.assertEqual(response.status, 400)
        form = aiohttp.FormData()
        form.add_field("file", b"x" * 50000, filename="big.bin")
        async with self.http.post(f"{self.base}/api/upload", data=form) as response:
            self.assertEqual(response.status, 413)
        self.assertFalse(any((self.web.data_dir / "uploads").glob("*/big.bin")))
        await ws.close()

    def test_the_store_keeps_shared_files_and_puts_unlisted_chats_after(self):
        from plugins_webui import Store
        store = Store(self.tmp / "web.db")
        try:
            for chat in ("classroom", "chat-a", "chat-b"):
                store.ensure(chat, chat)
            photo = self.tmp / "photo.png"
            photo.write_bytes(b"png")
            store.add_file("f1", photo, "photo.png", "image/png", 3)
            for chat in ("chat-a", "chat-b"):
                store.add(chat, {"kind": "user", "text": "look", "files": [{"id": "f1"}]})
            self.assertEqual(store.delete("chat-a"), [])  # chat-b still shows it
            self.assertIsNotNone(store.file("f1"))
            self.assertEqual(store.delete("chat-b"), [photo])
            store.ensure("chat-c", "chat-c")
            store.ensure("chat-d", "chat-d")
            store.order(["classroom", "chat-c"])
            self.assertEqual([chat["id"] for chat in store.conversations()], ["classroom", "chat-c", "chat-d"])
        finally:
            store.close()

    async def test_a_chat_whose_setup_a_crash_cut_off_is_repaired_before_its_first_message(self):
        spoke = await self.start_room()
        self.web.store.ensure("dm-gpt", "GPT", ["gpt"])  # the chat, but no assignment: setup never finished
        ws, _ = await self.socket()
        await self.say(ws, "just you and me", conversation="dm-gpt")  # before the start-up sweep runs
        await self.until(ws, lambda e: e["type"] == "message" and e["item"]["kind"] == "text"
                         and e["item"]["conversation"] == "dm-gpt")
        await settle(self.core)
        self.assertEqual(spoke, ["GPT"])
        self.assertEqual(self.assignment("dm-gpt")["pattern"], "webui:dm-gpt")
        await ws.close()

    async def test_a_chat_deleted_while_its_repair_waits_is_not_brought_back_by_a_queued_message(self):
        from contracts import SubmitResult
        spoke = await self.start_room()
        self.web.store.ensure("dm-gpt", "GPT", ["gpt"])  # needs its repair on the first message
        release, submit, held = asyncio.Event(), self.web.port.submit, []

        async def slow_core(request):  # the repair's setup is held until the chat is deleted elsewhere
            if request.text.startswith("$call core.set_assignment"):
                async def later():
                    await release.wait()
                    await submit(request)
                held.append(asyncio.create_task(later()))
                return SubmitResult(True, "accepted")
            return await submit(request)
        self.web.port.submit = slow_core
        a, _ = await self.socket()
        b, _ = await self.socket()
        await self.say(a, "only for GPT", conversation="dm-gpt", id="queued")
        await asyncio.sleep(0.1)
        await b.send_json({"type": "delete", "conversation": "dm-gpt"})
        await self.until(b, lambda e: e["type"] == "deleted")
        release.set()
        ack, _ = await self.until(a, lambda e: e["type"] == "ack")
        self.assertEqual((ack["ok"], ack["reason"]), (False, "unknown_chat"))
        await asyncio.gather(*held)
        for _ in range(60):
            if self.assignment("dm-gpt")["pattern"] == "webui:*" and not self.web.store.assigned():
                break
            await asyncio.sleep(0.05)
        await settle(self.core)
        self.assertEqual((spoke, self.web.store.conversation("dm-gpt")), ([], None))
        self.assertEqual((self.assignment("dm-gpt")["pattern"], self.web.store.assigned()), ("webui:*", []))
        await a.close()
        await b.close()

    async def test_deleting_a_chat_from_before_the_record_still_removes_its_assignment(self):
        await self.start_room()
        self.core.manager.set_assignment("webui:dm-old", {"engine": "room", "personas": ["gpt"]})  # (its old name)
        self.web.store.ensure("dm-old", "GPT", ["gpt"])  # made by an older version: not in `assigned`
        ws, _ = await self.socket()
        await ws.send_json({"type": "delete", "conversation": "dm-old"})
        await self.until(ws, lambda e: e["type"] == "deleted")
        for _ in range(60):
            if self.assignment("dm-old")["pattern"] == "webui:*":
                break
            await asyncio.sleep(0.05)
        self.assertEqual((self.assignment("dm-old")["pattern"], self.web.store.assigned()), ("webui:*", []))
        await ws.close()

    async def test_without_momochat_the_page_opens_greyed_and_says_so(self):
        paths = [path for path in make_config(self.tmp).raw["modules"]["paths"] if not path.endswith("momochat")]
        self.core = await start_core(self.tmp, {
            "extensions": [*paths, str(ROOT / "extensions" / "students"), str(PLUGIN)],
            "extension_config": {"webui": {"host": "127.0.0.1", "port": 0}}})
        self.web = iface(self.core, "webui")
        self.base = f"http://127.0.0.1:{self.web.port_number}"
        self.assertNotIn("webui:*", self.core.manager.assignments)  # its default named MomoChat: set aside
        ws, state = await self.socket()
        self.assertFalse(state["momochat"])
        view = next(item for item in state["here"]["views"] if item["name"] == "MomoChat")
        self.assertEqual((view.get("off"), state["here"]["place"]["note"]), (True, "not loaded"))
        await ws.close()

    async def test_momochat_is_the_default_for_its_conversations(self):
        from core.loader import import_extension
        extension, _ = import_extension(PLUGIN)

        class Context:
            name, data_dir = "webui", self.tmp
            config = {"port": 0}

            @staticmethod
            def log(*args, **kwargs):
                pass
        contributions = extension.setup(Context())
        self.assertEqual(contributions.assignments["webui:*"]["engine"], "momochat")
        self.assertEqual(contributions.assignments["webui:*"]["personas"],
                         ["gpt", "gemini", "claude", "deepseek", "grok"])
        self.assertFalse(contributions.admission["webui:*"]["require_mention_in_groups"])


if __name__ == "__main__":
    unittest.main()

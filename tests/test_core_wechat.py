"""WeChat extension tests: a fake account transport inside a real core (no desktop, no live database)."""

import asyncio
import hashlib
import itertools
import json
import hmac
import sqlite3
import random
import struct
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from contracts import Address, Reply
from core.app import Core
from tests.core_support import FIXTURES, ROOT, FakeAdapter, extension_module, iface, make_config, settle

ACCOUNT = "wxid_me_ab12"
ME = "wxid_me"
FRIEND = f"wechat:{ME}/wxid_friend"
ROOM = f"wechat:{ME}/room@chatroom"
QUIET = f"wechat:{ME}/quiet@chatroom"


def scratch() -> Path:
    # Inside the repository's ignored temp/ directory rather than the system temporary directory.
    path = ROOT / "temp" / "core_wechat_tests"
    path.mkdir(parents=True, exist_ok=True)
    return path


class FakeTransport:
    """The account transport's surface as the interface uses it, with scripted chats."""

    read_backend = "fake database"

    def __init__(self, directory: Path):
        self.directory = directory
        self.account_id, self.username = ACCOUNT, ME
        self.state = {"state": "ready", "account_id": ACCOUNT, "name": "Me", "detail": ""}
        self.chats: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.own_message_ids = deque(maxlen=200)
        self.local = itertools.count(1)
        self.busy: set[str] = set()
        self.outcome = "verified"
        self.images: dict[str, Path] = {}
        self.before_commit = None
        self.confirmed = 0
        self.checks = 0  # account checks (each one is a desktop helper run)
        self.closed = False

    def add_chat(self, native, name, kind="direct"):
        self.chats[native] = {"name": name, "kind": kind, "messages": []}

    def add(self, native, text, outgoing=False, sender="wxid_friend", name="Friend", mentions=False, count=None,
            kind="text", at=None, reply_to_server_id=None, image_md5=None):
        local = next(self.local)
        message = {"id": f"native:message/message_0.db:Msg_{native}:{local}", "chat_id": "native:" + native,
                   "sender_id": ME if outgoing else sender, "sender_name": "Me" if outgoing else name,
                   "text": text, "outgoing": outgoing, "kind": kind, "timestamp": at or time.time(),
                   "position": [local * 1000, local], "server_id": str(9000 + local),
                   "reply_to_server_id": reply_to_server_id, "chat_kind": self.chats[native]["kind"],
                   "mentions_me": mentions, "mention_count": count if count is not None else int(mentions),
                   "image_md5": image_md5}
        self.chats[native]["messages"].append(message)
        return message

    def chat(self, native):
        data = self.chats[native]
        last = data["messages"][-1]["timestamp"] if data["messages"] else 0
        return {"id": "native:" + native, "native_id": native, "name": data["name"], "chat_kind": data["kind"],
                "unread": 0, "preview": data["messages"][-1]["text"] if data["messages"] else "",
                "last_timestamp": last + len(data["messages"]) * 1e-6}

    async def status(self, refresh=False):
        self.checks += 1
        return dict(self.state)

    async def confirm_login(self):
        self.confirmed += 1
        self.state = {"state": "ready", "account_id": ACCOUNT, "name": "Me", "detail": ""}
        return dict(self.state)

    async def login(self):
        path = self.directory / "qr.png"
        path.write_bytes(b"png")
        return {"state": "logged_out", "path": str(path)}

    async def list_chats(self):
        return [self.chat(native) for native in self.chats]

    async def resolve(self, target):
        matches = [self.chat(native) for native, data in self.chats.items()
                   if target in ("native:" + native, native, data["name"])]
        if len(matches) != 1:
            raise ValueError("Native recipient is unavailable or ambiguous")
        return matches[0]

    async def read_messages(self, chat_id, limit=20):
        return [dict(item) for item in self.chats[chat_id.removeprefix("native:")]["messages"][-limit:]]

    def chat_busy(self, chat_id):
        return chat_id.removeprefix("native:") in self.busy

    async def image(self, chat_id, md5):
        return self.images.get(md5)

    async def send_checked(self, chat_id, text, prepare, authorize, on_commit, reply_to=None):
        native = chat_id.removeprefix("native:")
        if self.before_commit:
            self.before_commit(native)
        decision = await prepare(dict(self.state), await self.read_messages(chat_id, 100))
        if decision is not None:
            return decision
        authorize()
        on_commit()
        return self._commit(native, text, "text", reply_to)

    async def send_file(self, chat_id, path, filename=None, authorize=None):
        authorize()
        return self._commit(chat_id.removeprefix("native:"), filename, "file", None)

    def _commit(self, native, text, kind, reply_to):
        self.sent.append({"chat": native, "text": text, "kind": kind,
                          "quote": reply_to["server_id"] if reply_to else None})
        if self.outcome == "uncertain":
            return {"verified": False, "reason": "native_outgoing_receipt_ambiguous"}
        message = self.add(native, text if kind == "text" else "[File]", outgoing=True, kind=kind,
                           reply_to_server_id=reply_to["server_id"] if reply_to else None)
        self.own_message_ids.append(message["id"])
        return {"verified": True, "message_id": message["id"]}

    async def close(self):
        self.closed = True


class WeChatCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=scratch())
        self.tmp = Path(self._tmp.name)
        self.transport = FakeTransport(self.tmp)
        self.transport.add_chat("wxid_friend", "Friend")
        self.transport.add_chat("room@chatroom", "Room", "group")
        self.transport.add_chat("quiet@chatroom", "Quiet", "group")
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    def config(self, overrides=None, wechat=None):
        settings = {"database_root": "", "poll_seconds": 3600, "coalesce_seconds": 0, "account_interval_s": 0,
                    "chat_interval_s": 0, **(wechat or {})}
        return make_config(self.tmp, {
            "extensions": [str(ROOT / "plugins" / "wechat"), str(FIXTURES / "fake"), str(FIXTURES / "probe"),
                           str(FIXTURES / "tools"), str(ROOT / "extensions" / "basic"),
                           str(ROOT / "extensions" / "momochat")],
            "extension_config": {"wechat": settings},
            "admission": {"wechat:*": {"enabled": False}, FRIEND: {"enabled": True},
                          ROOM: {"enabled": True, "max_age_s": 900}},
            # These tests drive the transport with the probe engine (the module's default is MomoChat).
            "assignments": {"wechat:*": {"engine": "probe", "personas": ["p"]}},
            **(overrides or {})})

    async def start(self, overrides=None, wechat=None, adapter=None):
        self.core = Core(self.config(overrides, wechat), provider_adapters={"fake": adapter or FakeAdapter()},
                         tick_seconds=3600)
        await self.core.build()
        self.wechat = self.core.gateway.interfaces["wechat"]
        self.wechat.transport = self.transport
        await self.core.start()
        # Tests drive polling themselves.
        self.wechat._task.cancel()
        await asyncio.gather(self.wechat._task, return_exceptions=True)
        await self.poll()
        self.owner = iface(self.core, "owner")
        return self.core

    async def restart(self, **options):
        await self.core.stop()
        self.transport.closed = False
        return await self.start(**options)

    async def poll(self):
        await self.wechat.poll_once()
        await settle(self.core)

    def texts(self, native=None):
        return [item["text"] for item in self.transport.sent if native is None or item["chat"] == native]


class ReadingTests(WeChatCase):
    async def test_first_start_baselines_history_and_answers_only_new_messages(self):
        self.transport.add("wxid_friend", "echo old message")
        await self.start()
        self.assertEqual(self.texts(), [])
        self.transport.add("wxid_friend", "echo hello")
        await self.poll()
        self.assertEqual(self.texts("wxid_friend"), ["hello"])
        run = self.core.tank.runs(("done",))[0]
        self.assertEqual(run.address, FRIEND)
        trigger = run.trigger
        self.assertTrue(trigger["is_direct"])
        self.assertTrue(trigger["mentions_me"])
        self.assertEqual(trigger["sender"]["id"], "wxid_friend")
        self.assertFalse(trigger["display"]["markdown"])
        # The bot's own reply is recognised as its own: no manual-reply handling, no second answer.
        await self.poll()
        self.assertEqual(self.texts(), ["hello"])
        self.assertEqual(len(self.core.tank.runs(("done", "cancelled"))), 1)

    async def test_restart_catches_up_from_the_saved_cursor_without_replaying(self):
        await self.start()
        self.transport.add("wxid_friend", "echo before")
        await self.poll()
        await self.core.stop()
        self.transport.add("wxid_friend", "echo while stopped")
        await self.start()
        self.assertEqual(self.texts("wxid_friend"), ["before", "while stopped"])
        await self.restart()
        self.assertEqual(self.texts("wxid_friend"), ["before", "while stopped"])

    async def test_groups_report_wechat_mentions_and_strip_the_picked_mention(self):
        await self.start()
        self.transport.add("room@chatroom", "echo chatter")
        self.transport.add("room@chatroom", "@Me echo typed by hand")
        self.transport.add("room@chatroom", "@Me\u2005echo hi there", mentions=True)
        await self.poll()
        self.assertEqual(self.texts("room@chatroom"), ["hi there"])
        trigger = self.core.tank.runs(("done",))[0].trigger
        self.assertFalse(trigger["is_direct"])
        self.assertTrue(trigger["display"]["quotes"])
        self.assertEqual(trigger["text"], "echo hi there")
        history = [item.text for item in self.core.tank.recent_messages(ROOM, 10)]
        self.assertNotIn("echo chatter", history)

    async def test_chatter_can_be_recorded_as_history_when_configured(self):
        await self.start(wechat={"observe_chatter": True})
        self.transport.add("room@chatroom", "echo chatter")
        await self.poll()
        self.assertEqual(self.texts(), [])
        self.assertIn("echo chatter", [item.text for item in self.core.tank.recent_messages(ROOM, 10)])

    async def test_quoted_messages_carry_reply_to_and_quoted_text(self):
        await self.start()
        original = self.transport.add("wxid_friend", "the original")
        await self.poll()
        self.transport.add("wxid_friend", "facts", reply_to_server_id=original["server_id"])
        await self.poll()
        self.assertEqual(self.texts("wxid_friend")[-1], f"text|the original|{original['id']}")

    async def test_dollar_commands_work_for_contacts(self):
        await self.start()
        self.transport.add("wxid_friend", "$nosuchcommand")
        self.transport.add("room@chatroom", "@Me\u2005$help", mentions=True)
        await self.poll()
        self.assertEqual(self.texts("wxid_friend"), ["未知命令 $nosuchcommand。发送 $help 查看可用命令。"])
        self.assertTrue(self.texts("room@chatroom") and "$" in self.texts("room@chatroom")[0])

    async def test_conversations_that_are_not_admitted_are_never_answered(self):
        await self.start()
        self.transport.add("quiet@chatroom", "@Me\u2005echo nope", mentions=True)
        await self.poll()
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.core.tank.runs(("done",)), [])

    async def test_media_is_recorded_not_answered_unless_configured(self):
        await self.start()
        self.transport.add("wxid_friend", "[Image]", kind="image")
        await self.poll()
        self.assertEqual(self.texts(), [])
        self.assertEqual([(item.text, item.kind) for item in self.core.tank.recent_messages(FRIEND, 5)], [("[Image]", "image")])

    async def test_images_are_decrypted_into_history_even_before_the_mention(self):
        await self.start()
        photo = self.tmp / "abc.jpg"
        photo.write_bytes(b"\xff\xd8\xffjpeg")
        self.transport.images["abc"] = photo
        self.transport.add("room@chatroom", "[Image]", kind="image", image_md5="abc")
        self.transport.add("quiet@chatroom", "[Image]", kind="image", image_md5="abc")
        await self.poll()
        stored = self.core.tank.recent_messages(ROOM, 5)
        self.assertEqual([(item.kind, item.files[0].mime) for item in stored], [("image", "image/jpeg")])
        self.assertEqual(self.core.tank.recent_messages(QUIET, 5), [])

    async def test_a_burst_is_gathered_into_one_run(self):
        adapter = FakeAdapter([{"message": "收到三条", "status": "complete", "actions": []}])
        await self.start({"assignments": {"wechat:*": {"engine": "assistant", "personas": ["wechat"]},
                                          "*": {"engine": "probe", "personas": ["p"]}}},
                         wechat={"coalesce_seconds": 0.05}, adapter=adapter)
        for text in ("一", "二", "三"):
            self.transport.add("wxid_friend", text)
        await self.poll()
        self.assertEqual(self.texts(), [])
        await asyncio.sleep(0.06)
        await self.poll()
        self.assertEqual(self.texts("wxid_friend"), ["收到三条"])
        self.assertEqual(len(adapter.calls), 1)
        prompt = json.dumps(adapter.calls[0]["messages"], ensure_ascii=False)
        self.assertTrue(all(text in prompt for text in ("一", "二", "三")))
        self.assertIn("本鲸", prompt)
        self.assertEqual(len(self.core.tank.runs(("done",))), 1)

    async def test_busy_chats_are_skipped_until_their_receipt_finishes(self):
        await self.start()
        self.transport.busy.add("wxid_friend")
        self.transport.add("wxid_friend", "echo later")
        await self.poll()
        self.assertEqual(self.texts(), [])
        self.transport.busy.clear()
        await self.poll()
        self.assertEqual(self.texts(), ["later"])

    async def test_manual_reply_cancels_open_runs_and_is_recorded(self):
        await self.start()
        extension_module(self.core, "probe").RELEASE = asyncio.Event()
        self.transport.add("wxid_friend", "hold")
        await self.wechat.poll_once()
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(len(self.core.tank.runs(("running",))), 1)
        self.transport.add("wxid_friend", "I'll answer myself", outgoing=True)
        await self.poll()
        self.assertEqual(len(self.core.tank.runs(("cancelled",))), 1)
        history = self.core.tank.recent_messages(FRIEND, 5)
        self.assertEqual((history[-1].direction, history[-1].text), ("out", "I'll answer myself"))



class DeliveryTests(WeChatCase):
    async def test_group_replies_quote_the_trigger_and_long_text_is_split(self):
        await self.start()
        trigger = self.transport.add("room@chatroom", "@Me\u2005echo x", mentions=True)
        await self.poll()
        delivery = await self.wechat.deliver(Reply(Address.parse(ROOM), "text", "word " * 1000, quote=trigger["id"]))
        self.assertTrue(delivery.ok)
        parts = [item for item in self.transport.sent if item["chat"] == "room@chatroom"][1:]
        self.assertEqual(len(parts), 2)
        self.assertEqual([part["quote"] for part in parts], [trigger["server_id"], None])
        self.assertTrue(all(len(part["text"]) <= 4000 for part in parts))

    async def test_files_are_sent_and_recognised_as_own(self):
        await self.start()
        self.transport.add("wxid_friend", 'tool tools.make_file {"name": "a.txt"}')
        await self.poll()
        self.assertEqual([item["kind"] for item in self.transport.sent], ["text", "file"])
        await self.poll()
        self.assertEqual(self.core.tank.runs(("cancelled",)), [])

    async def test_run_failures_and_events_are_not_shown_to_contacts(self):
        await self.start()
        self.transport.add("wxid_friend", "crash")
        await self.poll()
        self.assertEqual(self.texts(), [])
        self.assertTrue(self.owner.notices("run_failed_detail"))

    async def test_manual_reply_during_delivery_cancels_the_pending_send(self):
        await self.start()
        self.transport.before_commit = lambda native: self.transport.add(native, "typed on the phone", outgoing=True)
        self.transport.add("wxid_friend", "echo stale")
        await self.poll()
        # The run cancels itself from inside its own delivery (port.cancel awaited in deliver).
        self.assertEqual(self.texts(), [])
        cancelled = self.core.tank.runs(("cancelled",))
        self.assertEqual(len(cancelled), 1)
        self.assertIn("manual_reply", cancelled[0].error)
        self.assertEqual(self.core.tank.runs(("running", "waiting_input", "waiting_approval")), [])
        self.assertEqual(self.core.manager._tasks, {})
        # The poller then records the manual reply without a second cancellation or a reply.
        self.transport.before_commit = None
        await self.poll()
        history = self.core.tank.recent_messages(FRIEND, 5)
        self.assertEqual((history[-1].direction, history[-1].text), ("out", "typed on the phone"))
        self.assertEqual(self.texts(), [])

    async def test_uncertain_send_is_reported_and_its_late_receipt_is_ours(self):
        await self.start()
        self.transport.outcome = "uncertain"
        delivery = await self.wechat.deliver(Reply(Address.parse(FRIEND), "text", "maybe"))
        self.assertFalse(delivery.ok)
        self.assertTrue(delivery.uncertain)
        await settle(self.core)
        events = [reply for reply in self.owner.delivered if reply.kind == "event"]
        self.assertEqual(events[-1].code, "send_unverified")
        self.transport.add("wxid_friend", "maybe", outgoing=True)
        await self.poll()
        history = self.core.tank.recent_messages(FRIEND, 5)
        self.assertEqual((history[-1].direction, history[-1].text), ("out", "maybe"))
        self.assertEqual(self.core.tank.runs(("cancelled",)), [])

    async def test_one_chats_long_interval_does_not_hold_up_other_chats_or_owner_sends(self):
        await self.start()
        self.wechat.port.bottle.set("send_intervals", {"wxid_friend": 3600})
        await self.wechat.deliver(Reply(Address.parse(FRIEND), "text", "first"))
        waiting = asyncio.create_task(self.wechat.deliver(Reply(Address.parse(FRIEND), "text", "an hour later")))
        await asyncio.sleep(0.05)
        delivery = await asyncio.wait_for(self.wechat.deliver(Reply(Address.parse(ROOM), "text", "other chat")), 2)
        self.assertTrue(delivery.ok)
        owner = await asyncio.wait_for(
            self.wechat.deliver(Reply(Address.parse(FRIEND), "text", "from the owner", source="owner")), 2)
        self.assertTrue(owner.ok)
        self.assertFalse(waiting.done())
        self.assertEqual(self.texts(), ["first", "other chat", "from the owner"])
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

    async def test_send_spacing_is_enforced_per_account(self):
        await self.start(wechat={"account_interval_s": 0.2})
        started = time.monotonic()
        await self.wechat.deliver(Reply(Address.parse(FRIEND), "text", "one"))
        await self.wechat.deliver(Reply(Address.parse(ROOM), "text", "two"))
        self.assertGreaterEqual(time.monotonic() - started, 0.19)


class OwnerTests(WeChatCase):
    async def command(self, text):
        await self.owner.say(text, conversation="dm", sender="boss")
        await settle(self.core)
        return self.owner.texts("dm")[-1] if self.owner.texts("dm") else ""

    async def test_operations_list_status_read_and_send(self):
        await self.start()
        self.transport.add("wxid_friend", "hello from friend")
        chats = await self.command("$wechat_chats")
        self.assertIn(f"Friend · direct · {FRIEND}", chats)
        self.assertIn("ready", await self.command("$wechat_status"))
        status = await self.command("$wechat_status Room")
        self.assertIn(ROOM, status)
        self.assertIn("'max_age_s': 900", status)
        self.assertIn("hello from friend", await self.command("$wechat_read Friend"))
        self.assertIn("shows your message", await self.command("$wechat_send Quiet | hi all"))
        self.assertEqual(self.texts("quiet@chatroom"), ["hi all"])
        # The chat that approvals and grants are about is the one the message goes to.
        mixed = await self.command('$call wechat.send {"chat": "Friend", "spec": "Quiet | sneaky"}')
        self.assertIn("not both", mixed)
        self.assertEqual(self.texts("quiet@chatroom"), ["hi all"])
        await self.poll()
        self.assertEqual(self.core.tank.runs(("cancelled",)), [])

    async def test_login_returns_the_qr_code_and_confirms_on_the_next_poll(self):
        self.transport.state = {"state": "logged_out", "account_id": "", "name": "", "detail": ""}
        await self.start()
        await self.command("$wechat_login")
        files = [reply for reply in self.owner.delivered if reply.kind == "file"]
        self.assertEqual(files[-1].file.name, "wechat-login.png")
        self.transport.state = {"state": "confirming", "account_id": "", "name": "", "detail": ""}
        await self.poll()
        self.assertEqual(self.transport.confirmed, 1)
        self.assertTrue(self.core.gateway.ready()["wechat"][0])

    async def test_readiness_changes_are_reported_and_sign_out_cancels_work(self):
        self.transport.state = {"state": "unavailable", "account_id": "", "name": "", "detail": "not running"}
        await self.start()
        codes = lambda: [reply.code for reply in self.owner.delivered if reply.kind == "event"]
        self.assertEqual(codes(), ["interface_not_ready"])
        self.transport.state = {"state": "confirming", "account_id": "", "name": "", "detail": ""}
        await self.poll()
        self.assertEqual(codes()[-1], "wechat_state")
        self.assertIn("confirmed on the phone", self.owner.delivered[-1].text)
        delivery = await self.core.gateway.deliver(Reply(Address.parse(FRIEND), "text", "x"))
        self.assertEqual(delivery.detail, "interface_not_ready")
        self.transport.state = {"state": "ready", "account_id": ACCOUNT, "name": "Me", "detail": ""}
        await self.poll()
        self.assertEqual(codes()[-1], "interface_ready")
        extension_module(self.core, "probe").RELEASE = asyncio.Event()
        self.transport.add("wxid_friend", "hold")
        await self.wechat.poll_once()
        for _ in range(20):
            await asyncio.sleep(0)
        self.transport.state = {"state": "logged_out", "account_id": "", "name": "", "detail": ""}
        await self.poll()
        self.assertEqual(len(self.core.tank.runs(("cancelled",))), 1)
        self.assertEqual(codes()[-1], "interface_not_ready")

    async def test_notifications_reach_the_owner_and_replies_are_relayed(self):
        # Quiet is not admitted: it is never answered, but the owner can follow it and reply.
        await self.start()
        deliveries = []
        original = self.owner.deliver

        async def recording(reply):
            delivery = await original(reply)
            deliveries.append((reply, delivery))
            return delivery
        self.owner.deliver = recording
        self.assertIn("on", await self.command("$wechat_notify on Quiet"))
        self.transport.add("quiet@chatroom", "anyone there?", sender="wxid_amy", name="Amy")
        self.transport.add("room@chatroom", "not followed", sender="wxid_amy", name="Amy")
        await self.poll()
        events = [(reply, delivery) for reply, delivery in deliveries if reply.kind == "event"]
        self.assertEqual([reply.text for reply, _ in events], ["WeChat · Quiet · Amy: anyone there?"])
        self.assertEqual(self.texts(), [])
        await self.owner.say("on my way", conversation="dm", sender="boss", reply_to=events[0][1].message_id)
        await settle(self.core)
        self.assertEqual(self.texts("quiet@chatroom"), ["on my way"])
        self.assertTrue(any(reply.code == "relayed" and reply.data.get("ok") == "true" for reply, _ in deliveries))
        # The relayed message shows up in the database as the bot's own, not as a manual reply.
        await self.poll()
        self.assertEqual(self.texts(), ["on my way"])
        self.assertEqual([item.direction for item in self.core.tank.recent_messages(QUIET, 5)], ["out"])

    async def test_a_long_wait_stops_the_checks_until_resume(self):
        # Each check runs the desktop helper: WeChat away for `give_up_s` in a row stops them, said once.
        self.transport.state = {"state": "unavailable", "account_id": "", "name": "", "detail": "not running"}
        await self.start(wechat={"give_up_s": 60})
        await self.poll()
        self.assertNotIn("stopped checking", self.owner.delivered[-1].text)
        self.wechat._unready_since -= 61
        await self.poll()
        told = [reply.text for reply in self.owner.delivered if "stopped checking" in reply.text]
        self.assertEqual(told, ["WeChat stopped checking: unavailable for 1 min (not running). Run $wechat_login to "
                                "set it up again, or $wechat_resume."])
        checks = self.transport.checks
        await self.poll()
        await self.poll()
        self.assertEqual(self.transport.checks, checks)
        self.assertEqual(len([reply for reply in self.owner.delivered if "stopped checking" in reply.text]), 1)
        self.assertIn("stopped checking", await self.command("$wechat_status"))
        self.transport.state = {"state": "ready", "account_id": ACCOUNT, "name": "Me", "detail": ""}
        await self.command("$wechat_resume")
        await self.poll()
        self.assertTrue(self.core.gateway.ready()["wechat"][0])
        self.transport.add("wxid_friend", "echo back again")
        await self.poll()
        self.assertEqual(self.texts(), ["back again"])

    async def test_login_checks_again_and_a_login_in_progress_never_gives_up(self):
        self.transport.state = {"state": "logged_out", "account_id": "", "name": "", "detail": ""}
        await self.start(wechat={"give_up_s": 60})
        self.wechat._unready_since -= 61
        await self.poll()
        self.assertTrue(self.wechat._gave_up)
        await self.command("$wechat_login")
        checks = self.transport.checks
        await self.poll()
        self.assertGreater(self.transport.checks, checks)
        self.wechat._unready_since -= 61  # (minutes go by before the phone scans the code)
        await self.poll()
        self.assertEqual(self.wechat._gave_up, "")
        self.transport.state = {"state": "confirming", "account_id": "", "name": "", "detail": ""}
        await self.poll()
        self.assertEqual(self.transport.confirmed, 1)
        self.assertTrue(self.core.gateway.ready()["wechat"][0])
        self.assertEqual(self.wechat._gave_up, "")

    async def test_pause_stops_reading_and_resume_continues_from_the_cursor(self):
        await self.start()
        await self.command("$wechat_pause")
        self.transport.add("wxid_friend", "echo while paused")
        await self.poll()
        self.assertEqual(self.texts(), [])
        await self.command("$wechat_resume")
        await self.poll()
        self.assertEqual(self.texts(), ["while paused"])


class ProductionPolicyTests(WeChatCase):
    async def test_documented_policy_answers_group_mentions_with_the_persona_and_quotes(self):
        adapter = FakeAdapter([{"message": "好呀", "status": "complete", "actions": []}])
        await self.start({
            "admission": {"*": {"enabled": True, "require_mention_in_groups": True}, "wechat:*": {"enabled": False},
                          ROOM: {"enabled": True, "max_age_s": 900}, FRIEND: {"enabled": True, "max_age_s": 900}},
            "assignments": {"*": {"engine": "probe", "personas": ["p"]},
                            "wechat:*": {"engine": "assistant", "personas": ["wechat"],
                                         "budget": {"model_calls": 24, "tool_calls": 24, "seconds": 1800},
                                         "lane": "sender",
                                         "options": {"history_scope": "sender", "quote": "trigger", "diary": False,
                                                     "max_steps": 12}}},
            "permissions": {"wechat:*": {"use": ["*", "!system.*"], "tiers": {"write": "always-ask"}}}},
            adapter=adapter)
        trigger = self.transport.add("room@chatroom", "@Me\u2005说句话", mentions=True, sender="wxid_amy", name="Amy")
        await self.poll()
        self.assertEqual([(item["text"], item["quote"]) for item in self.transport.sent],
                         [("好呀", trigger["server_id"])])
        run = self.core.tank.runs(("done",))[0]
        self.assertEqual(run.lane, f"{ROOM}#wxid_amy")
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertIn("本鲸", system)
        self.assertIn("Use plain text only", system)


# Encrypted SQLCipher page fixture (from tests/test_wechat_database_pages.py).
KEY = bytes(range(32))
SALT = bytes(range(16))


def sqlite_fixture(value='hello'):
    """Small valid SQLite database with SQLCipher's 80 reserved bytes per page."""
    def record(values):
        serial, body = [], b''
        for value in values:
            if isinstance(value,int):
                serial.append(1);body += bytes([value])
            else:
                raw=value.encode();serial.append(13+2*len(raw));body += raw
        return bytes([len(serial)+1,*serial])+body
    def page(payload, offset=0):
        result=bytearray(4096)
        cell=bytes([len(payload),1])+payload
        start=4016-len(cell)
        result[offset:offset+8]=struct.pack('>BHHHB',13,0,1,start,0)
        result[offset+8:offset+10]=start.to_bytes(2,'big')
        result[start:start+len(cell)]=cell
        return result
    schema=record(['table','records','records',2,'CREATE TABLE records(value TEXT)'])
    first=page(schema,100)
    first[:16]=b'SQLite format 3\0'
    first[16:24]=bytes((16,0,1,1,80,64,32,32))
    for offset,field_value in [(24,1),(28,2),(40,1),(44,4),(56,1),(92,1),(96,3040000)]:
        first[offset:offset+4]=field_value.to_bytes(4,'big')
    return bytes(first)+bytes(page(record([value])))


def encrypt(plain,number=1):
    offset=16 if number==1 else 0
    iv=bytes([number%256])*16
    encryptor=Cipher(algorithms.AES(KEY),modes.CBC(iv)).encryptor()
    cipher=encryptor.update(plain[offset:-80])+encryptor.finalize()
    mac_key=hashlib.pbkdf2_hmac('sha512',KEY,bytes(v^0x3a for v in SALT),2,32)
    mac=hmac.new(mac_key,cipher+iv+number.to_bytes(4,'little'),'sha512').digest()
    return (SALT if number==1 else b'')+cipher+iv+mac


def encrypted_fixture(value='hello'):
    plain=sqlite_fixture(value)
    return b''.join(encrypt(plain[i:i+4096],i//4096+1) for i in range(0,len(plain),4096))


class DormantTests(unittest.TestCase):
    def test_as_a_drop_in_it_does_nothing_until_configured(self):
        from types import SimpleNamespace
        from unittest import mock
        extension = __import__("core.loader", fromlist=["import_extension"]).import_extension(ROOT / "plugins" / "wechat")[0]
        with tempfile.TemporaryDirectory() as tmp:
            context = SimpleNamespace(config={}, data_dir=Path(tmp), log=lambda *args, **fields: None)
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(list(extension.setup(context).interfaces), [])
            context.config = {"database_root": ""}
            contributions = extension.setup(context)
            self.assertEqual(len(contributions.interfaces), 1)
            # Configured, it still answers no chat until core.yml opens one, and then as 本鲸.
            self.assertEqual(contributions.admission, {"wechat:*": {"enabled": False}})
            self.assertEqual(contributions.assignments["wechat:*"]["personas"], ["wechat"])


class CopiedModuleTests(unittest.TestCase):
    """The database modules were copied from services/; check the copies, not the originals."""

    def module(self, name):
        package = __import__("core.loader", fromlist=["import_extension"]).import_extension(
            ROOT / "plugins" / "wechat")[1].__name__.rsplit(".", 1)[0]
        return __import__(f"{package}.{name}", fromlist=["x"])

    def test_v2_images_decrypt_with_the_account_key(self):
        import struct
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        images = self.module("images")
        jpeg = b"\xff\xd8\xff" + bytes(range(256)) * 8 + b"\xff\xd9"
        key, xor, head_length = b"0123456789abcdef", 0x50, 1024
        padder = padding.PKCS7(128).padder()
        head = padder.update(jpeg[:head_length]) + padder.finalize()
        encrypted = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(head)
        tail = bytes(byte ^ xor for byte in jpeg[head_length:])
        data = images.V2 + struct.pack("<II", head_length, len(tail)) + b"\x01" + encrypted + tail
        self.assertEqual(images.decrypt(data, key, xor), jpeg)
        with self.assertRaises(images.ImageUnavailable):
            images.decrypt(data, None, xor)
        old = bytes(byte ^ 0x33 for byte in jpeg)
        self.assertEqual(images.decrypt(old, None, 0), jpeg)
        self.assertEqual(images.image_md5(b'\x08\t\x1a"" 5e8f575aab4b1dc27e23a9803009833eX\x00'),
                         "5e8f575aab4b1dc27e23a9803009833e")

    def test_snapshots_decrypt_and_tampering_fails_closed(self):
        pages = self.module("database_pages")
        plain = pages.decrypt_snapshot(encrypted_fixture("hi"), b"", KEY)
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.deserialize(plain)
        self.assertEqual(connection.execute("SELECT value FROM records").fetchone()[0], "hi")
        with self.assertRaises(pages.DatabaseSnapshotError):
            pages.decrypt_snapshot(encrypted_fixture("hi"), b"", bytes(32))

    def test_key_files_must_be_private_and_belong_to_the_account(self):
        keys = self.module("database_keys")
        with tempfile.TemporaryDirectory(dir=scratch()) as directory:
            path = Path(directory) / "keys.json"
            entry = {"path": "contact/contact.db", "key": "00" * 32, "profile": keys.PROFILE}
            document = {"account_directory": "wxid_a_1", "keys": {
                "11" * 16: entry, "22" * 16: {**entry, "path": "session/session.db"},
                "33" * 16: {**entry, "path": "message/message_0.db"}}}
            path.write_text(json.dumps(document))
            path.chmod(0o644)
            with self.assertRaises(keys.DatabaseSnapshotError):
                keys.load_keys(path, "wxid_a_1")
            path.chmod(0o600)
            self.assertEqual(len(keys.load_keys(path, "wxid_a_1")), 3)
            with self.assertRaises(keys.DatabaseSnapshotError):
                keys.load_keys(path, "wxid_b_2")


FAKE_HELPER = """
import json, sys
from pathlib import Path
request = json.loads(sys.stdin.readline())
mode = Path(sys.argv[0]).with_name("mode").read_text()
if mode == "refuse":
    print(json.dumps({"ok": False, "error": "The selected recipient changed; no send attempted"}))
    sys.exit()
print(json.dumps({"ok": True, "result": {"account": {"state": "ready", "account_id": "%s"}, "messages": []}}), flush=True)
commit = json.loads(sys.stdin.readline())
if not commit.get("commit"):
    print(json.dumps({"ok": True, "result": {"cancelled": True}}))
    sys.exit()
Path(sys.argv[0]).with_name("sent").write_text(commit["text"])
if mode == "crash":
    print(json.dumps({"ok": False, "error": "Desktop access failed (CalledProcessError)"}))
else:
    print(json.dumps({"ok": True, "result": {"sent": True, "verified": False}}))
""" % ACCOUNT


class FakeReader:
    account_id, username = ACCOUNT, ME

    def __init__(self, directory):
        self.directory = directory
        self.threads = set()

    def list_chats(self):
        return [{"id": "native:wxid_friend", "native_id": "wxid_friend", "name": "Friend", "chat_kind": "direct"}]

    def resolve(self, target):
        return self.list_chats()[0]

    def read_chat(self, chat, limit=20, fresh=False):
        self.threads.add(threading.current_thread().name)
        items = [{"id": "m1", "text": "hi", "outgoing": False, "kind": "text", "server_id": "1"}]
        sent = self.directory / "sent"
        if sent.exists():
            items.append({"id": "m2", "text": sent.read_text(), "outgoing": True, "kind": "text", "server_id": "2"})
        return items

    def close(self):
        pass


class TransportProtocolTests(unittest.IsolatedAsyncioTestCase):
    """The real transport's two-phase desktop protocol and native receipts, against a scripted helper."""

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=scratch())
        self.tmp = Path(self._tmp.name)
        package = __import__("core.loader", fromlist=["import_extension"]).import_extension(
            ROOT / "plugins" / "wechat")[1].__name__.rsplit(".", 1)[0]
        self.module = __import__(f"{package}.transport", fromlist=["WeChatTransport"])
        self.transport = self.module.WeChatTransport(
            {"python": sys.executable, "outbox_dir": str(self.tmp / "outbox"), "login_dir": str(self.tmp / "login")},
            reader=FakeReader(self.tmp))
        self.transport.helper = self.tmp / "helper.py"
        self.transport.helper.write_text(FAKE_HELPER)
        self.commits = 0

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def send(self, mode, decision=None):
        (self.tmp / "mode").write_text(mode)

        async def prepare(account, latest):
            return decision

        def commit():
            self.commits += 1
        return await self.transport.send_checked("Friend", "hello", prepare, lambda: None, commit)

    async def test_a_committed_send_is_verified_by_its_native_row(self):
        receipt = await self.send("ok")
        self.assertEqual((receipt["verified"], receipt["message_id"], self.commits), (True, "m2", 1))
        self.assertIn("m2", self.transport.own_message_ids)

    async def test_database_reads_run_on_the_reader_thread_not_the_event_loop(self):
        await self.transport.read_messages("Friend")
        await self.send("ok")
        threads = self.transport.reader.threads
        self.assertTrue(threads)
        self.assertTrue(all(name.startswith("wechat-reader") for name in threads), threads)
        await self.transport.close()
        self.assertTrue(self.transport._executor._shutdown)

    async def test_a_cancelled_prepare_never_commits(self):
        receipt = await self.send("ok", {"cancelled": True, "reason": "manual_reply"})
        self.assertEqual(receipt, {"cancelled": True, "reason": "manual_reply"})
        self.assertFalse((self.tmp / "sent").exists())
        self.assertEqual(self.commits, 0)

    async def test_errors_are_classified_as_not_sent_or_uncertain(self):
        with self.assertRaises(self.module.SendFailed):
            await self.send("refuse")
        with self.assertRaises(self.module.SendUncertain):
            await self.send("crash")
        self.assertIsInstance(self.module.classify(RuntimeError("Draft could not be verified; no send attempted")),
                              self.module.SendFailed)


class SplitTests(unittest.TestCase):
    def test_every_chunk_fits_the_limit(self):
        package = __import__("core.loader", fromlist=["import_extension"]).import_extension(
            ROOT / "plugins" / "wechat")[1].__name__.rsplit(".", 1)[0]
        split_text = __import__(f"{package}.interface", fromlist=["split_text"]).split_text
        self.assertEqual([len(chunk) for chunk in split_text("a" * 4000 + " " + "b" * 100)], [4000, 101])
        self.assertEqual([len(chunk) for chunk in split_text("a" * 3999 + "\n\n" + "b" * 10)], [4000, 11])
        generator = random.Random(7)
        for _ in range(300):
            limit = generator.choice([5, 17, 64, 4000])
            text = "".join(generator.choice("ab \n") for _ in range(generator.randint(1, limit * 6)))
            chunks = split_text(text, limit)
            self.assertTrue(all(len(chunk) <= limit for chunk in chunks), (limit, [len(c) for c in chunks]))
            self.assertEqual("".join(chunks).replace(" ", "").replace("\n", ""),
                             text.replace(" ", "").replace("\n", ""))


class SettingsAndReaderTests(unittest.TestCase):
    def test_settings_fall_back_to_the_environment(self):
        module = __import__("core.loader", fromlist=["import_extension"]).import_extension(
            ROOT / "plugins" / "wechat")[1]
        settings = module.settings_from({"poll_seconds": 0.01}, Path("/data"), {
            "WECHAT_BRIDGE_DATABASE_ROOT": "/x/wxid_a_1/db_storage", "WECHAT_BRIDGE_MAX_UPLOAD_MB": "250"})
        self.assertEqual(settings["database_root"], "/x/wxid_a_1/db_storage")
        self.assertEqual(settings["max_upload_mb"], 100)
        self.assertEqual(settings["poll_seconds"], 0.2)
        self.assertEqual(settings["database_keys"], "/data/keys.json")

    def test_native_reader_reports_mention_counts_and_strips_one_mention(self):
        native = __import__("core.loader", fromlist=["import_extension"]).import_extension(
            ROOT / "plugins" / "wechat")[1]
        package = native.__name__.rsplit(".", 1)[0]
        reader_module = __import__(package + ".native", fromlist=["WeChatNativeReader"])
        reader = reader_module.WeChatNativeReader.__new__(reader_module.WeChatNativeReader)
        reader.root = Path("/nonexistent-wechat-fixture")
        reader.username, reader.account_id = "wxid_self", "wxid_self_account"
        connections = {name: sqlite3.connect(":memory:") for name in
                       ["contact/contact.db", "session/session.db", "message/message_0.db"]}
        for connection in connections.values():
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
        connections["contact/contact.db"].execute("CREATE TABLE contact(username,remark,nick_name)")
        connections["contact/contact.db"].executemany("INSERT INTO contact VALUES (?,?,?)", [
            ("wxid_self", "", "Me"), ("sender", "", "Friend"), ("room@chatroom", "", "Group")])
        connections["session/session.db"].execute(
            "CREATE TABLE SessionTable(username,unread_count,summary,last_timestamp,is_hidden,sort_timestamp)")
        connections["session/session.db"].execute("INSERT INTO SessionTable VALUES ('room@chatroom',1,'p',100,0,100)")
        messages = connections["message/message_0.db"]
        table = "Msg_" + hashlib.md5(b"room@chatroom").hexdigest()
        messages.execute("CREATE TABLE Name2Id(user_name)")
        messages.executemany("INSERT INTO Name2Id VALUES (?)", [("wxid_self",), ("sender",)])
        messages.execute(f'CREATE TABLE "{table}"(local_id,local_type,real_sender_id,create_time,sort_seq,'
                         'message_content,source,WCDB_CT_source,WCDB_CT_message_content)')
        messages.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?,?,?)', [
            (1, 1, 2, 100, 1, "sender:\n@Me\u2005hello", "<msgsource><atuserlist>wxid_self</atuserlist></msgsource>", 0, 0),
            (2, 1, 2, 101, 2, "sender:\n@Me\u2005@Amy\u2005hi", "<msgsource><atuserlist>wxid_self,amy</atuserlist></msgsource>", 0, 0),
            (3, 1, 1, 102, 3, "mine", "<msgsource/>", 0, 0)])
        reader.connection = lambda relative, fresh=False: connections[relative]
        reader.databases = {name: None for name in connections}
        items = reader.read_messages("Group")
        self.assertEqual([(m["mentions_me"], m["mention_count"], m["outgoing"]) for m in items],
                         [(True, 1, False), (True, 2, False), (False, 0, True)])
        self.assertEqual(reader_module.strip_mention(items[0]["text"]), "hello")
        self.assertIs(reader.list_chats()[0]["name"], reader.list_chats()[0]["name"])
        self.assertEqual(reader.resolve("native:room@chatroom")["chat_kind"], "group")


if __name__ == "__main__":
    unittest.main()

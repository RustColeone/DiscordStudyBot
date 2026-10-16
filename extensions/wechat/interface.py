"""WeChat interface: the account's conversations through the native database and the desktop client.

Conversations are `<account>/<native username>`, for example `wxid_abc/123@chatroom` for a group or
`wxid_abc/wxid_friend` for a direct chat, so policy written for one account never applies to another.
New messages are read from the account's local databases (read-only snapshots); replies, files, login
and account status go through the desktop helper. The interface reports facts only (`is_direct`,
`mentions_me` from WeChat's own mention entities); admission rules and assignments decide everything else.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Any

from contracts import (Address, Delivery, Display, FileRef, Interface, Operation, Reply, Request, Risk, Sender,
                       ToolResult)

from .images import mime_for
from .native import strip_mention
from .transport import SendFailed, WeChatTransport

SEND_LIMIT = 4000
NOTICES = {
    "waiting_approval": "⏳ 这个操作需要主人批准，请稍等。",
    "unknown_command": "未知命令 ${command}。发送 $help 查看可用命令。",
    "not_permitted": "这里不能使用这个命令。",
}
STATES = {
    "logged_out": "WeChat is logged out. Run $wechat_login and scan the QR code.",
    "confirming": "WeChat is waiting for the login to be confirmed on the phone. Run $wechat_login to continue.",
    "unavailable": "WeChat is not running or not readable ({detail}). Run $wechat_login to open it.",
    "identity_unknown": "WeChat's account cannot be verified ({detail}); replies are paused.",
}


def split_text(text: str, limit: int = SEND_LIMIT) -> list[str]:
    chunks, remaining = [], text
    while len(remaining) > limit:
        cut = 0
        for separator in ("\n\n", "\n", " "):
            # The chunk keeps its separator, so the separator must end within the limit.
            boundary = remaining.rfind(separator, 0, limit - len(separator) + 1)
            if boundary >= limit // 2:
                cut = boundary + len(separator)
                break
        cut = cut or limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining or not chunks:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk.strip()] or chunks[:1]


def position(message) -> tuple[int, int]:
    return tuple(message["position"])


class WeChatInterface(Interface):
    name = "wechat"
    operations = (
        Operation("status", "WeChat account, reader and polling status; with `chat`, that conversation's address, "
                  "admission, assignment and notification settings",
                  {"chat": {"type": "string", "description": "Chat name, native ID or address"}},
                  command="wechat_status", command_argument="chat"),
        Operation("login", "Open WeChat if needed and return the login QR code, or confirm a pending login",
                  risk=Risk.WRITE, command="wechat_login"),
        Operation("chats", "List WeChat conversations with their addresses",
                  {"query": {"type": "string", "description": "Optional part of the name"}},
                  command="wechat_chats", command_argument="query"),
        Operation("messages", "Read recent messages of one WeChat conversation",
                  {"chat": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                  ("chat",), command="wechat_read", command_argument="chat"),
        Operation("send", "Send a text message as the owner to a WeChat conversation (cancels its open runs)",
                  {"chat": {"type": "string"}, "text": {"type": "string"},
                   "spec": {"type": "string", "description": "$wechat_send form: `chat | text`"}},
                  risk=Risk.WRITE, command="wechat_send", command_argument="spec"),
        Operation("notify", "Tell the owner about incoming messages of a conversation (`*` = all): on, off or inherit",
                  {"chat": {"type": "string"}, "mode": {"type": "string", "enum": ["on", "off", "inherit"]},
                   "spec": {"type": "string", "description": "$wechat_notify form: `on|off|inherit chat`"}},
                  risk=Risk.WRITE, command="wechat_notify", command_argument="spec"),
        Operation("configure", "Set the minimum seconds between the bot's messages in one conversation",
                  {"chat": {"type": "string"}, "send_interval_s": {"type": "number", "minimum": 5, "maximum": 3600}},
                  ("chat", "send_interval_s"), risk=Risk.WRITE),
        Operation("pause", "Pause reading and sending without logging out", risk=Risk.WRITE, command="wechat_pause"),
        Operation("resume", "Resume reading and sending", risk=Risk.WRITE, command="wechat_resume"),
    )

    def __init__(self, settings: dict, transport: Any = None, clock=time.time):
        self.settings = dict(settings)
        self.display = Display(markdown=False, edits=False, progress=False, quotes=True, files=True,
                               max_upload_mb=float(self.settings["max_upload_mb"]),
                               max_text_chars=int(self.settings["max_text_chars"]))
        self.transport = transport
        self.port = None
        self._clock = clock
        self._task: asyncio.Task | None = None
        self._setup_error = ""
        self._account: dict = {"state": "starting"}
        self._reported: tuple[bool, str] | None = None
        self._signatures: dict[str, tuple] = {}
        self._gather: dict[str, tuple] = {}
        self._first_pass_done = False
        self._started_at = clock()
        self._spacing = asyncio.Lock()
        self._last_send = -1e9
        self._last_chat_send: dict[str, float] = {}
        self._uncertain: dict[str, list[dict]] = defaultdict(list)
        self._names: dict[str, str] = {}
        self._login_until = 0.0
        self._last_error = ""
        self._error_notified: dict[str, float] = {}

    # ---- lifecycle --------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        if self.transport is None:
            try:
                self.transport = WeChatTransport(self.settings, log=port.log)
            except Exception as error:
                self._setup_error = f"{type(error).__name__}: {error}"
                print(f"[wechat] Not connected: {self._setup_error}", flush=True)
                port.log("setup_failed", error=type(error).__name__)
                port.set_ready(False, f"WeChat is not configured: {self._setup_error}")
                return
        port.set_owners([self.transport.username])
        self._task = asyncio.create_task(self._poll_loop(), name="wechat-poll")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.transport is not None:
            await self.transport.close()


    # ---- addresses --------------------------------------------------------------------------------
    def address(self, native_id: str) -> Address:
        return Address(self.name, f"{self.transport.username}/{native_id}")

    def native_id(self, address: Address) -> str | None:
        account, separator, native = address.conversation.partition("/")
        if not separator or not native or self.transport is None or account != self.transport.username:
            return None
        return native

    def _display(self, group: bool) -> Display:
        return Display(markdown=False, edits=False, progress=False, quotes=group, files=True,
                       max_upload_mb=self.display.max_upload_mb, max_text_chars=self.display.max_text_chars)

    def _admitted(self, address: Address) -> bool:
        return bool(self.port.policy(address)["admission"].get("enabled", True))

    # ---- readiness --------------------------------------------------------------------------------
    @property
    def paused(self) -> bool:
        return bool(self.port is not None and self.port.bottle.get("paused"))

    async def _report(self, account: dict) -> bool:
        state = account.get("state", "unavailable")
        if self.paused:
            ready, detail, state = False, "WeChat is paused by the owner. Run $wechat_resume.", "paused"
        elif state == "ready":
            ready, detail = True, f"WeChat is logged in as {account.get('name') or self.transport.username}."
        else:
            ready = False
            detail = STATES.get(state, "WeChat is not ready ({detail}).").format(
                detail=account.get("detail") or state)
        previous, self._reported = self._reported, (ready, state)
        if previous == (ready, state):
            return ready
        if previous is not None and previous[0] and state in ("logged_out", "identity_unknown"):
            await self.port.cancel_all(f"account {state}")
        if previous is None or previous[0] != ready:
            self.port.set_ready(ready, detail)
        elif not ready:
            # Still not ready, but for a new reason (the client restarted and now waits for the phone).
            await self.port.notify(detail, code="wechat_state", data={"state": state})
        self.port.log("account_state", state=state)
        return ready

    # ---- reading ----------------------------------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
                self._last_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._poll_failed(error)
            await asyncio.sleep(float(self.settings["poll_seconds"]))

    async def _poll_failed(self, error: Exception) -> None:
        text = f"{type(error).__name__}: {error}"[:300]
        self._last_error = text
        self.port.log("poll_failed", error=type(error).__name__)
        now = time.monotonic()
        if now - self._error_notified.get(text, -1e9) >= 3600:
            self._error_notified[text] = now
            print(f"[wechat] Reading failed: {text}", flush=True)
            await self.port.notify(f"WeChat reading failed: {text}", code="wechat_error")

    async def poll_once(self) -> None:
        if self.paused:
            await self._report({"state": "paused"})
            return
        account = await self.transport.status()
        if account.get("state") == "confirming" and time.monotonic() < self._login_until:
            await self.transport.confirm_login()
            account = await self.transport.status(refresh=True)
        self._account = account
        if not await self._report(account):
            return
        failure = None
        for chat in await self.transport.list_chats():
            self._names[chat["native_id"]] = chat["name"]
            try:
                await self._poll_chat(chat)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # One unreadable conversation must not stop the others; its cursor stays where it was.
                failure = failure or error
        self._first_pass_done = True
        if failure is not None:
            raise failure

    async def _poll_chat(self, chat: dict) -> None:
        native = chat["native_id"]
        signature = (chat.get("last_timestamp"), chat.get("preview"), chat.get("unread"))
        if self._signatures.get(native) == signature and native not in self._gather:
            return
        bottle = self.port.bottle
        cursor = bottle.get(f"cursor:{native}")
        if (cursor is not None and native not in self._signatures and native not in self._gather
                and isinstance(chat.get("last_timestamp"), (int, float)) and chat["last_timestamp"] < cursor[2]):
            self._signatures[native] = signature
            return
        if self.transport.chat_busy(chat["id"]):
            # A file receipt can take minutes; read this chat after it.
            return
        messages = await self.transport.read_messages(chat["id"], 40)
        if cursor is None:
            if not self._first_pass_done:
                # First sight on the very first start: the current position is history, not new input.
                self._save_cursor(native, messages[-1] if messages else None)
                self._signatures[native] = signature
                return
            # A conversation that appeared while running: only what arrived since this process started.
            fresh = [item for item in messages if (item.get("timestamp") or 0) >= self._started_at - 5]
        else:
            fresh = [item for item in messages if position(item) > tuple(cursor[:2])]
            if messages and fresh and len(fresh) == len(messages):
                self.port.log("catch_up_window_full", read=len(messages))
        if not fresh:
            self._signatures[native] = signature
            return
        incoming = any(item.get("outgoing") is False and item.get("kind") == "text" for item in fresh)
        wait = float(self.settings["coalesce_seconds"])
        if incoming and wait > 0:
            # A short gathering window: a burst is submitted together and lands in one run.
            mark, now = self._gather.get(native), time.monotonic()
            if mark is None or mark[0] != position(fresh[-1]):
                self._gather[native] = (position(fresh[-1]), now)
                return
            if now - mark[1] < wait:
                return
        self._gather.pop(native, None)
        await self._process(chat, fresh, messages)
        self._signatures[native] = signature

    def _save_cursor(self, native: str, message: dict | None) -> None:
        value = [*position(message), int(message.get("timestamp") or 0)] if message else [0, 0, 0]
        self.port.bottle.set(f"cursor:{native}", value)

    async def _process(self, chat: dict, fresh: list[dict], window: list[dict]) -> None:
        native = chat["native_id"]
        address = self.address(native)
        group = chat.get("chat_kind") == "group"
        by_server = {item["server_id"]: item for item in window if item.get("server_id")}
        notes = []
        for item in fresh:
            if item.get("outgoing") is True:
                await self._outgoing(address, native, item)
            elif item.get("outgoing") is False:
                if await self._incoming(chat, address, group, item, by_server) and self._notify_on(native):
                    notes.append(item)
            # Messages are submitted in order and the cursor follows each one, so a crash never skips one.
            self._save_cursor(native, item)
        # Notifications go out after all submits, so a burst still reaches one run together.
        for item in notes:
            sender = f" · {item.get('sender_name') or item.get('sender_id')}" if group else ""
            await self.port.notify(f"WeChat · {chat['name']}{sender}: {item.get('text', '')[:1200]}", origin=address,
                                   code="incoming", data={"chat": chat["name"]})

    async def _incoming(self, chat: dict, address: Address, group: bool, item: dict, by_server: dict) -> bool:
        """Submit or record one incoming message. Returns whether the owner may be notified about it."""
        text = item.get("text") or ""
        kind = item.get("kind") or "text"
        if kind == "unsupported":
            kind = "other"
        mentioned = True if not group else item.get("mentions_me")
        sender = Sender(item.get("sender_id") or "unknown", item.get("sender_name") or "")
        files = ()
        if kind == "image" and item.get("image_md5") and self._admitted(address):
            path = await self.transport.image(chat["id"], item["image_md5"])
            if path is not None:
                files = (FileRef(str(path), Path(path).name, mime_for(Path(path)), Path(path).stat().st_size),)
        if kind != "text" and not self.settings["submit_media"]:
            # Images are kept even unaddressed: people send a picture first and ask about it next.
            if self._admitted(address) and (not group or mentioned or files or self.settings["observe_chatter"]):
                await self.port.observe(address, text, direction="in", sender=sender, kind=kind,
                                        message_id=item["id"], at=item.get("timestamp"), files=files)
            return True
        if not text.strip():
            return False
        if group and mentioned and item.get("mention_count") == 1:
            text = strip_mention(text) or text
        quoted = by_server.get(item.get("reply_to_server_id")) if item.get("reply_to_server_id") else None
        request = Request(
            id=item["id"], address=address, sender=sender, text=text, received_at=float(item.get("timestamp") or 0),
            is_direct=not group, mentions_me=mentioned, reply_to=quoted["id"] if quoted else None,
            quoted_text=(quoted.get("text") or "")[:1000] if quoted else "", kind=kind, display=self._display(group),
            files=files,
            extra={"chat_name": chat["name"], "server_id": str(item.get("server_id") or "")})
        result = await self.port.submit(request)
        if result and group:
            self._remember_quote(item)
        elif not result and result.reason == "not_mentioned" and self.settings["observe_chatter"]:
            await self.port.observe(address, text, direction="in", sender=sender, kind=kind, message_id=item["id"],
                                    at=request.received_at, reply_to=request.reply_to, quoted_text=request.quoted_text)
        return True

    async def _outgoing(self, address: Address, native: str, item: dict) -> None:
        if self._is_own(native, item):
            return
        late = self._late_receipt(native, item)
        if late is not None:
            await self.port.observe(address, item.get("text") or "", direction="out", kind=item.get("kind") or "text",
                                    message_id=item["id"], at=item.get("timestamp"), reply_to=late.get("reply_to"))
            self._remember_own(item["id"])
            return
        # Someone used the account directly (phone or desktop): pending replies here are stale.
        self._last_chat_send[native] = time.monotonic()
        if not self._admitted(address):
            return
        await self.port.observe(address, item.get("text") or "", direction="out",
                                sender=Sender(self.transport.username, self._account.get("name") or ""),
                                kind=item.get("kind") or "text", message_id=item["id"], at=item.get("timestamp"))
        await self.port.cancel(address, "manual_reply")

    # ---- own messages and quotes ------------------------------------------------------------------
    def _is_own(self, native: str, item: dict) -> bool:
        return item["id"] in set(self.port.bottle.get("own") or []) or item["id"] in set(
            getattr(self.transport, "own_message_ids", ()))

    def _remember_own(self, message_id: str) -> None:
        own = [value for value in (self.port.bottle.get("own") or []) if value != message_id]
        self.port.bottle.set("own", (own + [message_id])[-300:])

    def _late_receipt(self, native: str, item: dict) -> dict | None:
        """An unverified send that shows up later is ours, not a manual reply."""
        now = self._clock()
        pending = [entry for entry in self._uncertain.get(native, []) if now - entry["at"] < 900]
        for entry in pending:
            if (entry["text"] == item.get("text")) if entry["kind"] == "text" else item.get("kind") != "text":
                pending.remove(entry)
                self._uncertain[native] = pending
                return entry
        self._uncertain[native] = pending
        return None

    def _remember_quote(self, item: dict) -> None:
        quotes = OrderedDict(self.port.bottle.get("quotes") or {})
        quotes[item["id"]] = {key: item.get(key) for key in ("id", "text", "sender_id", "server_id", "kind")}
        quotes[item["id"]]["outgoing"] = False
        while len(quotes) > 300:
            quotes.popitem(last=False)
        self.port.bottle.set("quotes", dict(quotes))

    # ---- settings in the bottle -------------------------------------------------------------------
    def _notify_on(self, native: str) -> bool:
        modes = self.port.bottle.get("notify") or {}
        mode = modes.get(native, "inherit")
        return (modes.get("*", "off") if mode == "inherit" else mode) == "on"

    def _send_interval(self, native: str) -> float:
        intervals = self.port.bottle.get("send_intervals") or {}
        return float(intervals.get(native, self.settings["chat_interval_s"]))

    # ---- delivery ---------------------------------------------------------------------------------
    def render(self, reply: Reply) -> str:
        if reply.kind == "notice":
            if reply.code == "reminder":
                return "⏰ " + reply.text
            if reply.code in NOTICES:
                return NOTICES[reply.code].format(**{"command": "", **dict(reply.data)})
            # Failures and owner-side notices are not for contacts; the owner gets details elsewhere.
            return "" if reply.code in ("run_failed", "relayed", "run_failed_detail", "approval_request") else reply.text
        if reply.speaker:
            return f"[{reply.speaker}{' · ' + reply.label if reply.label else ''}] {reply.text}"
        return reply.text

    async def deliver(self, reply: Reply) -> Delivery:
        if self.transport is None:
            return Delivery(False, detail="not_connected")
        native = self.native_id(reply.address)
        if native is None:
            return Delivery(False, detail="unknown_conversation")
        if reply.kind in ("progress", "event"):
            return Delivery(True, detail="suppressed")
        if reply.source == "owner":
            # An explicit owner send takes the conversation over, as a manual reply would.
            await self.port.cancel(reply.address, "owner_send")
        if reply.kind == "file":
            delivery = await self._send_file(native, reply)
            if delivery.ok and reply.text.strip():
                caption = await self._send_text(native, reply.text.strip(), reply, None)
                if not caption.ok:
                    return Delivery(False, delivery.message_id, caption.detail, uncertain=True)
            return delivery
        text = self.render(reply).strip()
        if not text:
            return Delivery(True, detail="suppressed")
        first = None
        for index, part in enumerate(split_text(text)):
            delivery = await self._send_text(native, part, reply, reply.quote if index == 0 else None)
            if not delivery.ok:
                return Delivery(False, first, delivery.detail, uncertain=delivery.uncertain or index > 0)
            first = first or delivery.message_id
        return Delivery(True, message_id=first)

    def _spacing_wait(self, native: str) -> float:
        now = time.monotonic()
        return max(0.0, self._last_send + float(self.settings["account_interval_s"]) - now,
                   self._last_chat_send.get(native, -1e9) + self._send_interval(native) - now)

    @contextlib.asynccontextmanager
    async def _slot(self, native: str, reply: Reply):
        """Hold the account's send slot once this chat's spacing has passed.

        The wait itself happens without the lock, so one chat's long interval never delays another chat.
        Owner sends are explicit and are not spaced (the transport still serializes desktop calls).
        """
        if reply.source == "owner":
            yield
            return
        while True:
            await self._spacing.acquire()
            wait = self._spacing_wait(native)
            if wait <= 0:
                break
            self._spacing.release()
            await asyncio.sleep(wait)
        try:
            yield
        finally:
            self._spacing.release()

    def _mark_sent(self, native: str) -> None:
        self._last_send = self._last_chat_send[native] = time.monotonic()

    def _authorizer(self, reply: Reply):
        def authorize():
            reason = self.port.check(reply)
            if reason:
                raise SendFailed(reason)
        return authorize

    async def _send_text(self, native: str, text: str, reply: Reply, quote: str | None) -> Delivery:
        target = (self.port.bottle.get("quotes") or {}).get(quote) if quote else None
        address = reply.address

        async def prepare(account, latest):
            if account.get("state") != "ready" or account.get("account_id") != self.transport.account_id:
                return {"cancelled": True, "reason": "account_not_ready"}
            cursor = self.port.bottle.get(f"cursor:{native}") or [0, 0, 0]
            manual = [item for item in latest if item.get("outgoing") is True and position(item) > tuple(cursor[:2])
                      and not self._is_own(native, item)
                      and not any(entry["text"] == item.get("text") for entry in self._uncertain.get(native, []))]
            if manual and reply.source != "owner":
                # Someone answered by hand: the reply being sent (and any other open run here) is stale.
                await self.port.cancel(address, "manual_reply")
                return {"cancelled": True, "reason": "manual_reply"}
            reason = self.port.check(reply)
            return {"cancelled": True, "reason": reason} if reason else None

        async with self._slot(native, reply):
            reason = self.port.check(reply)
            if reason:
                return Delivery(False, detail=reason)
            try:
                receipt = await self._shielded(self.transport.send_checked(
                    f"native:{native}", text, prepare, self._authorizer(reply), lambda: self._mark_sent(native),
                    reply_to=target))
            except (SendFailed, ValueError) as error:
                return Delivery(False, detail=str(error)[:300])
            except Exception as error:
                # SendUncertain, or anything unexpected after the helper started: it may have been sent.
                return await self._unverified(native, reply, "text", text, f"{type(error).__name__}: {error}")
        if receipt.get("cancelled"):
            return Delivery(False, detail=receipt.get("reason") or "cancelled")
        if not receipt.get("verified"):
            return await self._unverified(native, reply, "text", text, receipt.get("reason") or "unverified")
        self._remember_own(receipt["message_id"])
        return Delivery(True, message_id=receipt["message_id"])

    async def _send_file(self, native: str, reply: Reply) -> Delivery:
        async with self._slot(native, reply):
            reason = self.port.check(reply)
            if reason:
                return Delivery(False, detail=reason)
            # The upload continues outside the spacing lock; its slot is taken now.
            self._mark_sent(native)
        file = reply.file
        try:
            receipt = await self._shielded(self.transport.send_file(
                f"native:{native}", file.path, file.name or Path(file.path).name, authorize=self._authorizer(reply)))
        except (SendFailed, ValueError) as error:
            return Delivery(False, detail=str(error)[:300])
        except Exception as error:
            return await self._unverified(native, reply, "file", None, f"{type(error).__name__}: {error}")
        if not receipt.get("verified"):
            return await self._unverified(native, reply, "file", None, receipt.get("reason") or "unverified")
        self._remember_own(receipt["message_id"])
        return Delivery(True, message_id=receipt["message_id"])

    async def _shielded(self, coroutine) -> dict:
        """A send that has begun finishes even if its run is cancelled meanwhile; its receipt is still ours."""
        task = asyncio.ensure_future(coroutine)

        def remember(done: asyncio.Task) -> None:
            if not done.cancelled() and done.exception() is None and (done.result() or {}).get("verified"):
                self._remember_own(done.result()["message_id"])
        task.add_done_callback(remember)
        return await asyncio.shield(task)

    async def _unverified(self, native: str, reply: Reply, kind: str, text: str | None, detail: str) -> Delivery:
        self._uncertain[native].append({"kind": kind, "text": text, "reply_to": reply.in_reply_to, "at": self._clock()})
        self.port.log("send_unverified", kind=kind)
        name = self._names.get(native, native)
        await self.port.notify(f"WeChat · {name}: a {'file' if kind == 'file' else 'message'} may have been sent but "
                               f"could not be verified ({detail[:200]}). It will not be retried; check WeChat.",
                               origin=reply.address, code="send_unverified")
        return Delivery(False, detail=detail[:300], uncertain=True)

    # ---- operations -------------------------------------------------------------------------------
    async def _resolve(self, chat: str) -> dict:
        chat = (chat or "").strip()
        if chat.startswith(self.name + ":"):
            chat = chat[len(self.name) + 1:]
        if "/" in chat:
            account, _, chat = chat.partition("/")
            if account != self.transport.username:
                raise ValueError("That address belongs to a different WeChat account")
        return await self.transport.resolve(chat)

    async def operate(self, operation, arguments, context) -> ToolResult:
        if self.transport is None:
            return ToolResult.fail(f"WeChat is not connected: {self._setup_error or 'not started'}", "not_connected")
        try:
            handler = getattr(self, f"_op_{operation}", None)
            if handler is None:
                return await super().operate(operation, arguments, context)
            return await handler(arguments, context)
        except (ValueError, RuntimeError) as error:
            return ToolResult.fail(str(error)[:500], type(error).__name__)

    async def _op_status(self, arguments, context) -> ToolResult:
        if arguments.get("chat"):
            chat = await self._resolve(arguments["chat"])
            native = chat["native_id"]
            address = self.address(native)
            policy = self.port.policy(address)
            assignment = policy["assignment"] or {}
            modes = self.port.bottle.get("notify") or {}
            cursor = self.port.bottle.get(f"cursor:{native}")
            return ToolResult.ok(
                f"WeChat · {chat['name']} ({chat['chat_kind']})\nAddress: {address}\n"
                f"Admission: {policy['admission']}\n"
                f"Assignment: {assignment.get('engine', 'none')} {assignment.get('personas', [])}"
                f"{'' if assignment.get('enabled', True) else ' (disabled)'}\n"
                f"Notifications: {'on' if self._notify_on(native) else 'off'} ({modes.get(native, 'inherit')})\n"
                f"Minimum send interval: {self._send_interval(native):g}s\n"
                f"Read position: {cursor[:2] if cursor else 'none'}",
                data={"address": str(address), "admission": policy["admission"]})
        account = await self.transport.status(refresh=True)
        cursors = len(list(self.port.bottle.keys("cursor:")))
        state = "paused" if self.paused else account.get("state")
        return ToolResult.ok(
            f"WeChat: {state} · account {account.get('name') or self.transport.username} "
            f"({self.transport.account_id})\nReader: {self.transport.read_backend}; polling every "
            f"{self.settings['poll_seconds']:g}s; {cursors} conversations tracked\n"
            f"Send spacing: {self.settings['account_interval_s']:g}s account, {self.settings['chat_interval_s']:g}s "
            f"per chat; gathering window {self.settings['coalesce_seconds']:g}s\n"
            + (f"Detail: {account.get('detail')}\n" if account.get("detail") else "")
            + (f"Last reading error: {self._last_error}" if self._last_error else "Reading: OK"),
            data={"state": state})

    async def _op_login(self, arguments, context) -> ToolResult:
        self._login_until = time.monotonic() + 180
        result = await self.transport.login()
        path = result.get("path")
        if path:
            file = FileRef(path, "wechat-login.png", "image/png", Path(path).stat().st_size)
            return ToolResult.ok("Scan this WeChat QR code and confirm on the phone. Login completes when WeChat "
                                 "reports ready; run $wechat_login again if the code expires.", files=[file])
        return ToolResult.ok(f"WeChat: {result.get('state')}. {result.get('detail') or ''}".strip())

    async def _op_chats(self, arguments, context) -> ToolResult:
        query = (arguments.get("query") or "").strip()
        chats = [chat for chat in await self.transport.list_chats() if query in chat["name"]][:50]
        lines = [f"{chat['name']} · {chat['chat_kind']} · {self.address(chat['native_id'])}"
                 + (f" · {chat['unread']} unread" if chat.get("unread") else "") for chat in chats]
        return ToolResult.ok("WeChat conversations:\n" + ("\n".join(lines) or "(none)"),
                             data={"addresses": [str(self.address(chat["native_id"])) for chat in chats]})

    async def _op_messages(self, arguments, context) -> ToolResult:
        chat = await self._resolve(arguments["chat"])
        messages = await self.transport.read_messages(chat["id"], int(arguments.get("limit") or 20))
        lines = []
        for item in messages:
            who = "Me" if item.get("outgoing") else (item.get("sender_name") or item.get("sender_id") or "?")
            stamp = time.strftime("%m-%d %H:%M", time.localtime(item.get("timestamp") or 0))
            lines.append(f"{stamp} {who}: {item.get('text', '')}")
        return ToolResult.ok(f"WeChat · {chat['name']}\n" + ("\n".join(lines) or "(no messages)"))

    async def _op_send(self, arguments, context) -> ToolResult:
        chat_name, text = arguments.get("chat"), arguments.get("text")
        if arguments.get("spec"):
            chat_name, separator, text = arguments["spec"].partition("|")
            if not separator:
                return ToolResult.fail("Use: $wechat_send <chat> | <text>", "invalid_arguments")
        if not (chat_name or "").strip() or not (text or "").strip():
            return ToolResult.fail("Give a chat and the text to send.", "invalid_arguments")
        chat = await self._resolve(chat_name)
        address = self.address(chat["native_id"])
        reply = Reply(address, "text", text=text.strip(), source="owner")
        delivery = await self.deliver(reply)
        if delivery.ok:
            await self.port.observe(address, text.strip(), direction="out",
                                    sender=Sender(context.sender.id, context.sender.name),
                                    message_id=delivery.message_id)
            return ToolResult.ok(f"WeChat shows your message in {chat['name']}.",
                                 data={"message_id": delivery.message_id})
        code = "delivery_uncertain" if delivery.uncertain else "delivery_failed"
        return ToolResult.fail(f"Not confirmed in {chat['name']}: {delivery.detail}. It was not retried.", code)

    async def _op_notify(self, arguments, context) -> ToolResult:
        mode, chat_name = arguments.get("mode"), arguments.get("chat")
        if arguments.get("spec"):
            mode, _, chat_name = arguments["spec"].strip().partition(" ")
        mode, chat_name = (mode or "").strip().lower(), (chat_name or "*").strip() or "*"
        if mode not in ("on", "off", "inherit"):
            return ToolResult.fail("Use: $wechat_notify on|off|inherit <chat or *>", "invalid_arguments")
        modes = dict(self.port.bottle.get("notify") or {})
        if chat_name == "*":
            key, label = "*", "all conversations"
            mode = "off" if mode == "inherit" else mode
        else:
            chat = await self._resolve(chat_name)
            key, label = chat["native_id"], chat["name"]
        modes[key] = mode
        self.port.bottle.set("notify", modes)
        return ToolResult.ok(f"WeChat notifications for {label}: {mode}. They go to the control route, or to a route "
                             f"for these addresses with kinds [event]; reply to one to answer in WeChat.")

    async def _op_configure(self, arguments, context) -> ToolResult:
        chat = await self._resolve(arguments["chat"])
        intervals = dict(self.port.bottle.get("send_intervals") or {})
        intervals[chat["native_id"]] = float(arguments["send_interval_s"])
        self.port.bottle.set("send_intervals", intervals)
        return ToolResult.ok(f"Minimum send interval in {chat['name']}: {float(arguments['send_interval_s']):g}s.")

    async def _op_pause(self, arguments, context) -> ToolResult:
        self.port.bottle.set("paused", True)
        await self._report({"state": "paused"})
        return ToolResult.ok("WeChat reading and sending are paused; the account stays logged in. $wechat_resume "
                             "continues from the saved read positions.")

    async def _op_resume(self, arguments, context) -> ToolResult:
        self.port.bottle.set("paused", False)
        return ToolResult.ok("WeChat resumes with the next poll.")

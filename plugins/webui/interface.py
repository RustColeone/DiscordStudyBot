"""The web UI interface: a chat page on the local network, served by the bot itself.

Whoever opens it is the bot's creator (like the console), so it answers only the local network, refuses
pages from other sites and DNS-rebinding hostnames, and can ask for a passphrase once per browser.
Messages travel over one WebSocket per open page; the page's transcript is kept in `webui.db`.
"""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import re
import secrets
import shutil
import socket
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from contracts import Address, Delivery, Display, FileRef, Interface, Reply, Request, Sender

from .store import Store

DEFAULT_CONVERSATION = "classroom"
CONVERSATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
AVATAR_KEY = re.compile(r"^[a-z0-9_-]{1,40}$")
AVATAR_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif"}
# Shown in the page itself; anything else is downloaded, never rendered (an HTML or SVG file could run script).
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "audio/mpeg", "audio/ogg", "audio/wav",
                "audio/webm", "audio/mp4", "video/mp4", "video/webm", "application/pdf"}
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
DECISION = re.compile(r"^\$(approve|deny)\s+([0-9a-f]{4,12})(\s+always)?\s*$", re.I)
# The member list's model menu asks the core with `$model` in the background (see _quiet_command).
PERSONA = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MODEL = re.compile(r"^(reset|[A-Za-z0-9._-]{1,40}/[A-Za-z0-9._:/@+-]{1,160})$")
COOKIE = "webui_session"
KATEX = "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/"  # pinned; the page checks its integrity hashes
CSP = (f"default-src 'self'; script-src 'self' {KATEX}katex.min.js; style-src 'self' 'unsafe-inline' {KATEX}; "
       f"font-src 'self' data: {KATEX}; "
       "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'; object-src 'none'")


def local_remote(remote: str | None, networks: list) -> bool:
    """A peer on this machine or the local network (or in the configured networks)."""
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote.split("%", 1)[0])
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if networks:
        return any(address in network for network in networks)
    return address.is_loopback or address.is_private or address.is_link_local


def local_host(host: str | None, hostnames: set[str]) -> bool:
    """The Host header names this machine: an IP address, localhost, a local name or a configured one. A
    public name pointing here is a DNS-rebinding attempt from some website."""
    if not host:
        return False
    host = host.strip().lower()
    if host.startswith("[") and "]" in host:
        name = host[1:host.index("]")]  # [IPv6]:port
    elif host.count(":") == 1:
        name = host.split(":", 1)[0]  # name:port
    else:
        name = host  # a bare name, or a bare IPv6 address
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return name == "localhost" or name.endswith(LOCAL_SUFFIXES) or name in hostnames


def avatar_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:40]


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the outgoing interface
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class WebUI(Interface):
    def __init__(self, name: str, config: dict, data_dir: Path, folder: Path):
        self.name = name
        self.folder = folder
        self.data_dir = Path(data_dir)
        self.host = str(config.get("host", "0.0.0.0"))
        self.port_number = int(config.get("port", 4156))
        self.passphrase = str(config.get("passphrase") or "").strip()
        self.user_id = str(config.get("user_id", "creator"))
        self.user_name = str(config.get("user_name") or getpass.getuser() or "You")
        self.title = str(config.get("title", "Study Room"))
        self.classmates = [str(item) for item in config.get("classmates")
                           or ["Luna", "Gemini", "Claude", "DeepSeek", "Grok"]]
        self.hostnames = {str(item).lower() for item in config.get("hostnames") or []}
        self.networks = [ipaddress.ip_network(str(item), strict=False) for item in config.get("networks") or []]
        upload_mb = float(config.get("max_upload_mb", 50))
        self.display = Display(markdown=True, edits=False, progress=True, quotes=False, files=True,
                               max_upload_mb=upload_mb, max_text_chars=int(config.get("max_text_chars", 20000)))
        self.port = None
        self.store: Store | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._progress: dict[str, dict] = {}  # run (or conversation) → the latest progress event
        self._failures: dict[str, deque] = {}
        self._quiet: dict[str, dict] = {}  # request ID → {conversation, purpose}: the page's own `$model` asks
        self._runner: web.AppRunner | None = None
        self.address_shown = ""

    # ---- lifecycle -------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        self.store = Store(self.data_dir / "webui.db")
        self.store.ensure(DEFAULT_CONVERSATION, "Classroom")
        # Whoever opens the page is the creator; approvals they start come back to the page. With no other
        # interface offering one, the classroom also becomes the control route.
        port.set_owners([self.user_id], control=Address(self.name, DEFAULT_CONVERSATION), fallback=True)
        app = web.Application(middlewares=[self._guard],
                              client_max_size=int(self.display.max_upload_mb * 1024 * 1024) + 65536)
        app.add_routes([
            web.get("/", self._index), web.get("/login", self._login_page), web.post("/login", self._login),
            web.get("/static/{name}", self._static), web.get("/avatars/{key}", self._avatar),
            web.get("/favicon.ico", self._favicon),
            web.get("/files/{file_id}", self._file), web.get("/api/history", self._history),
            web.post("/api/upload", self._upload), web.get("/ws", self._socket)])
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port_number)
        try:
            await site.start()
        except OSError as error:
            await self._runner.cleanup()
            self._runner = None
            port.set_ready(False, f"cannot listen on {self.host}:{self.port_number}: {error.strerror or error}")
            raise
        if self._runner.addresses:
            self.port_number = int(self._runner.addresses[0][1])
        shown = lan_address() if self.host in ("0.0.0.0", "::", "") else self.host
        self.address_shown = f"http://{shown}:{self.port_number}"
        print(f"[{self.name}] Open {self.address_shown} on this network"
              + (" (passphrase required)." if self.passphrase else "."), flush=True)
        port.log("listening", address=self.address_shown, passphrase=bool(self.passphrase))

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self.store is not None:
            self.store.close()
            self.store = None

    # ---- guard: local network, this host, this page, passphrase --------------------------------------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        if not local_remote(request.remote, self.networks):
            return self._secured(web.Response(status=403, text="Only the local network may use this page."))
        if not local_host(request.headers.get("Host"), self.hostnames):
            return self._secured(web.Response(status=403, text="Open this page by the computer's address."))
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"{request.scheme}://{request.headers.get('Host')}":
            # Another site in this browser trying to use the page (or its WebSocket) on the user's behalf.
            return self._secured(web.Response(status=403, text="Cross-site requests are refused."))
        public = request.path in ("/login", "/static/style.css", "/favicon.ico")
        if self.passphrase and not public and not self._signed_in(request):
            if request.method == "GET" and request.path == "/":
                raise web.HTTPSeeOther("/login")
            return self._secured(web.Response(status=401, text="Sign in first."))
        return self._secured(await handler(request))

    @staticmethod
    def _secured(response):
        if isinstance(response, web.WebSocketResponse):
            return response
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    def _session_value(self) -> str:
        key_file = self.data_dir / "session.key"
        if not key_file.is_file():
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(secrets.token_bytes(32))
            key_file.chmod(0o600)
        # Changing the passphrase signs every browser out.
        return hmac.new(key_file.read_bytes(), self.passphrase.encode(), hashlib.sha256).hexdigest()

    def _signed_in(self, request: web.Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), self._session_value())

    async def _login_page(self, request: web.Request, error: str = "") -> web.Response:
        page = (self.folder / "static" / "login.html").read_text(encoding="utf-8")
        page = page.replace("{{title}}", self._html(self.title)).replace("{{error}}", self._html(error))
        return web.Response(text=page, content_type="text/html", status=401 if error else 200)

    async def _login(self, request: web.Request) -> web.Response:
        if not self.passphrase:
            raise web.HTTPSeeOther("/")
        remote = request.remote or ""
        failures = self._failures.setdefault(remote, deque(maxlen=10))
        while failures and time.monotonic() - failures[0] > 300:
            failures.popleft()
        if len(failures) >= 5:
            return await self._login_page(request, "Too many tries; wait a few minutes.")
        form = await request.post()
        if not hmac.compare_digest(str(form.get("passphrase", "")).encode(), self.passphrase.encode()):
            failures.append(time.monotonic())
            await asyncio.sleep(1)
            self.port.log("login_failed", remote=remote)
            return await self._login_page(request, "That passphrase is not right.")
        response = web.HTTPSeeOther("/")
        response.set_cookie(COOKIE, self._session_value(), max_age=30 * 86400, httponly=True, samesite="Strict",
                            path="/")
        raise response

    @staticmethod
    def _html(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

    # ---- pages and files ----------------------------------------------------------------------------
    async def _index(self, request: web.Request) -> web.Response:
        page = (self.folder / "static" / "index.html").read_text(encoding="utf-8")
        return web.Response(text=page.replace("{{title}}", self._html(self.title)), content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    async def _static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        path = self.folder / "static" / name
        if not re.fullmatch(r"[a-z0-9_-]+\.(js|css)", name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def _favicon(self, request: web.Request) -> web.StreamResponse:
        path = self.folder / "static" / "favicon.png"
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "image/png", "Cache-Control": "max-age=86400"})

    def avatars(self) -> list[str]:
        folder = self.folder / "avatars"
        if not folder.is_dir():
            return []
        return sorted({path.stem.lower() for path in folder.iterdir()
                       if path.suffix.lower() in AVATAR_TYPES and AVATAR_KEY.fullmatch(path.stem.lower())})

    async def _avatar(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"].lower()
        if AVATAR_KEY.fullmatch(key):
            for path in (self.folder / "avatars").glob("*"):
                if path.stem.lower() == key and path.suffix.lower() in AVATAR_TYPES:
                    return web.FileResponse(path, headers={"Content-Type": AVATAR_TYPES[path.suffix.lower()],
                                                           "Cache-Control": "max-age=300"})
        raise web.HTTPNotFound()

    async def _file(self, request: web.Request) -> web.StreamResponse:
        record = self.store.file(request.match_info["file_id"])
        if record is None or not Path(record["path"]).is_file():
            raise web.HTTPNotFound()
        inline = record["mime"] in INLINE_TYPES
        disposition = "inline" if inline else "attachment"
        quoted = re.sub(r'["\\\r\n]', "_", record["name"])
        return web.FileResponse(record["path"], headers={
            "Content-Type": record["mime"] if inline else "application/octet-stream",
            "Content-Disposition": f'{disposition}; filename="{quoted}"', "Cache-Control": "private, max-age=86400"})

    def _keep_file(self, source: Path, name: str, folder: str) -> dict:
        file_id = uuid.uuid4().hex
        safe = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(name or source.name).name).strip(" .") or "file"
        target = self.data_dir / folder / file_id / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        mime = mimetypes.guess_type(safe)[0] or "application/octet-stream"
        size = target.stat().st_size
        self.store.add_file(file_id, target, safe, mime, size)
        return {"id": file_id, "name": safe, "mime": mime, "size": size}

    async def _upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        kept = []
        async for part in reader:
            if part.filename is None:
                continue
            file_id = uuid.uuid4().hex
            safe = re.sub(r"[^\w.\- ()\[\]]+", "_", Path(part.filename).name).strip(" .") or "file"
            target = self.data_dir / "uploads" / file_id / safe
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open("wb") as handle:
                while chunk := await part.read_chunk(1 << 16):
                    size += len(chunk)
                    handle.write(chunk)
            mime = mimetypes.guess_type(safe)[0] or part.headers.get("Content-Type") or "application/octet-stream"
            self.store.add_file(file_id, target, safe, mime, size)
            kept.append({"id": file_id, "name": safe, "mime": mime, "size": size})
        return web.json_response({"files": kept})

    async def _history(self, request: web.Request) -> web.Response:
        conversation = request.query.get("conversation", DEFAULT_CONVERSATION)
        if not CONVERSATION_ID.fullmatch(conversation):
            raise web.HTTPBadRequest()
        try:
            after = int(request.query.get("after") or 0)
        except ValueError:
            raise web.HTTPBadRequest() from None
        return web.json_response({"messages": self.store.messages(conversation, after)})

    # ---- the live connection --------------------------------------------------------------------------
    def state(self) -> dict:
        commands = [{"name": command.name, "description": command.description, "usage": command.usage(),
                     "examples": [example for example in command.examples if example]}
                    for command in self.port.commands()]
        return {"title": self.title, "user": {"id": self.user_id, "name": self.user_name},
                "classmates": self.classmates, "avatars": self.avatars(), "commands": commands,
                "conversations": self.store.conversations(), "progress": list(self._progress.values()),
                "default": DEFAULT_CONVERSATION, "max_upload_mb": self.display.max_upload_mb}

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            await ws.send_json({"type": "hello", "state": self.state()})
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    event = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    await self._handle(ws, event)
        finally:
            self._sockets.discard(ws)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, event: dict) -> None:
        kind = event.get("type")
        if kind == "order":
            ids = [str(item) for item in list(event.get("conversations") or [])[:500]
                   if CONVERSATION_ID.fullmatch(str(item))]
            self.store.order(ids)
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
            return
        conversation = str(event.get("conversation") or "")
        if not CONVERSATION_ID.fullmatch(conversation):
            return
        if kind == "new":
            self.store.ensure(conversation, str(event.get("title") or "New chat")[:80])
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        elif kind == "rename":
            if self.store.conversation(conversation):
                self.store.rename(conversation, str(event.get("title") or "").strip()[:80] or conversation)
                await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        elif kind == "pin":
            self.store.pin(conversation, bool(event.get("pinned")))
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        elif kind == "delete":
            await self._delete(conversation)
        elif kind == "send":
            await self._send(ws, conversation, event)
        elif kind == "models":  # the member list's model menu: who answers with what, and what is offered
            await self._quiet_command(ws, conversation, "$model", "overview")
        elif kind == "model":  # a choice from that menu (or `reset`)
            persona, model = str(event.get("persona") or ""), str(event.get("model") or "")
            if PERSONA.fullmatch(persona) and MODEL.fullmatch(model):
                await self._quiet_command(ws, conversation, f"$model {persona} {model}", "choose")

    async def _quiet_command(self, ws: web.WebSocketResponse, conversation: str, text: str, purpose: str) -> None:
        """A `$command` the page sends for itself: it is not shown in the chat, and its answer comes back to the
        page as a `models` event (with the command's result data) instead of a bubble. The core still sees and
        records it like a typed command (the room leaves commands out of what its characters read)."""
        key = f"web-quiet-{uuid.uuid4().hex}"
        while len(self._quiet) >= 100:  # answers that never came (the core was restarted, say)
            self._quiet.pop(next(iter(self._quiet)))
        self._quiet[key] = {"conversation": conversation, "purpose": purpose}
        result = await self.port.submit(Request(
            id=key, address=Address(self.name, conversation), sender=Sender(self.user_id, self.user_name), text=text,
            received_at=time.time(), is_direct=True, mentions_me=True, display=self.display))
        if not result.accepted:
            self._quiet.pop(key, None)
            await ws.send_json({"type": "models", "conversation": conversation, "purpose": purpose, "ok": False,
                                "code": result.reason, "text": "", "result": {}})

    async def _delete(self, conversation: str) -> None:
        """Remove a chat from the page: its open work is cancelled and its transcript and files are deleted.
        (The core keeps its own history of the conversation, as for any other interface.)"""
        if self.store.conversation(conversation) is None:
            return
        await self.port.cancel(Address(self.name, conversation), "chat deleted in the web UI")
        kept = {(self.data_dir / "files").resolve(), (self.data_dir / "uploads").resolve()}
        for path in self.store.delete(conversation):
            if path.resolve().parent.parent in kept:  # only the file's own folder under files/ or uploads/
                shutil.rmtree(path.parent, ignore_errors=True)
        for key in [key for key, event in self._progress.items() if event["conversation"] == conversation]:
            self._progress.pop(key, None)
        if not self.store.conversations():
            self.store.ensure(DEFAULT_CONVERSATION, "Classroom")
        self.port.log("chat_deleted")
        await self._broadcast({"type": "deleted", "conversation": conversation,
                               "items": self.store.conversations()})

    async def _send(self, ws: web.WebSocketResponse, conversation: str, event: dict) -> None:
        request_id = str(event.get("id") or "")[:64] or uuid.uuid4().hex
        text = str(event.get("text") or "")[: self.display.max_text_chars]
        files = []
        for file_id in list(event.get("files") or [])[:10]:
            record = self.store.file(str(file_id))
            if record is not None:
                files.append(record)
        if not text.strip() and not files:
            return
        record = self.store.conversation(conversation) or self.store.ensure(conversation, "New chat")
        if record["title"] == "New chat" and text.strip() and not text.startswith("$"):
            self.store.rename(conversation, text.strip().splitlines()[0][:48])
        refs = tuple(FileRef(item["path"], item["name"], item["mime"], item["size"]) for item in files)
        images = bool(refs) and all(item["mime"].startswith("image/") for item in files)
        result = await self.port.submit(Request(
            id=f"web-{request_id}", address=Address(self.name, conversation), sender=Sender(self.user_id, self.user_name),
            text=text, received_at=time.time(), is_direct=True, mentions_me=True, display=self.display, files=refs,
            kind="text" if text.strip() else "image" if images else "file"))
        shown = [{key: item[key] for key in ("id", "name", "mime", "size")} for item in files]
        if result.accepted or result.reason == "duplicate":
            if result.reason != "duplicate":
                item = self.store.add(conversation, {"kind": "user", "text": text, "files": shown,
                                                     "sender": self.user_name, "request_id": request_id,
                                                     "action": str(event.get("action") or "")[:40]})
                await self._broadcast({"type": "message", "item": item})
                await self._mark_decision(text)
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        await ws.send_json({"type": "ack", "id": request_id, "ok": bool(result.accepted or result.reason == "duplicate"),
                            "reason": result.reason})

    async def _mark_decision(self, text: str) -> None:
        match = DECISION.match(text.strip())
        if match is None:
            return
        decision = "approved" if match.group(1).lower() == "approve" else "denied"
        if decision == "approved" and match.group(3):
            decision = "always allowed"
        item = self.store.decide(match.group(2).lower(), decision)
        if item is not None:
            await self._broadcast({"type": "update", "item": item})

    async def _broadcast(self, event: dict) -> None:
        async def send(ws):
            try:
                await asyncio.wait_for(ws.send_json(event), 5)
            except Exception:
                self._sockets.discard(ws)
        await asyncio.gather(*(send(ws) for ws in list(self._sockets)))

    # ---- delivery ------------------------------------------------------------------------------------
    async def _answer_quietly(self, reply: Reply, quiet: dict) -> Delivery:
        """An answer to one of the page's own commands: to the page as data, not into the chat."""
        if reply.kind == "progress" or (reply.kind == "notice" and reply.code == "waiting_approval"):
            return Delivery(True, message_id=f"quiet-{reply.id}")  # the answer itself comes later
        self._quiet.pop(reply.in_reply_to, None)
        try:
            result = json.loads(reply.data.get("result") or "{}")
        except ValueError:
            result = {}
        await self._broadcast({"type": "models", "conversation": quiet["conversation"], "purpose": quiet["purpose"],
                               "ok": reply.kind == "text", "code": reply.code or "", "text": reply.text,
                               "result": result if isinstance(result, dict) else {}})
        return Delivery(True, message_id=f"quiet-{reply.id}")

    async def deliver(self, reply: Reply) -> Delivery:
        if self.store is None:
            return Delivery(False, detail="not_started")
        conversation = reply.address.conversation
        if not CONVERSATION_ID.fullmatch(conversation):
            conversation = DEFAULT_CONVERSATION
        if self.store.conversation(conversation) is None:
            self.store.ensure(conversation, "Notices" if conversation.startswith("_") else conversation)
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        quiet = self._quiet.get(reply.in_reply_to or "")
        if quiet is not None:
            return await self._answer_quietly(reply, quiet)
        key = reply.run_id or conversation
        if reply.kind == "progress":
            event = {"type": "progress", "conversation": conversation, "run_id": key, "text": reply.text,
                     "actions": [action.__dict__ for action in reply.actions], "at": time.time()}
            self._progress[key] = event
            await self._broadcast(event)
            return Delivery(True, message_id=f"progress-{key}")
        item: dict[str, Any] = {"kind": reply.kind, "text": reply.text, "speaker": reply.speaker,
                                "label": reply.label, "run_id": reply.run_id, "code": reply.code,
                                "data": dict(reply.data), "actions": [action.__dict__ for action in reply.actions],
                                "origin": reply.data.get("origin_address", ""), "reply_id": reply.id}
        if reply.kind == "file" and reply.file is not None:
            source = Path(reply.file.path)
            if not source.is_file():
                return Delivery(False, detail="missing_file")
            item["file"] = self._keep_file(source, reply.file.name, "files")
        if reply.run_id and self._progress.pop(reply.run_id, None) is not None:
            # Whatever a run says next (an answer, a file, an approval card) replaces its "typing…" line.
            await self._broadcast({"type": "progress_clear", "conversation": conversation, "run_id": reply.run_id})
        item = self.store.add(conversation, item)
        await self._broadcast({"type": "message", "item": item})
        return Delivery(True, message_id=str(item["id"]))

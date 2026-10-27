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
DEFAULT_TITLE = "The club"  # the group chat every page opens first (its id stays "classroom")
CONVERSATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
AVATAR_KEY = re.compile(r"^[a-z0-9_-]{1,40}$")
ROOM_FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}\.(png|jpg|jpeg|webp|gif)$")  # a preset room picture in rooms/
AVATAR_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                ".gif": "image/gif"}
# Shown in the page itself; anything else is downloaded, never rendered (an HTML or SVG file could run script).
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "audio/mpeg", "audio/ogg", "audio/wav",
                "audio/webm", "audio/mp4", "video/mp4", "video/webm", "application/pdf"}
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
DECISION = re.compile(r"^\$(approve|deny)\s+([0-9a-f]{4,12})(\s+always)?\s*$", re.I)
# The member list's model menu asks the core with `$model` in the background (see _quiet_command).
MODEL_PURPOSES = ("overview", "choose")
QUIET_PREFIX = "web-quiet-"  # the request IDs of the page's own commands
DISPLAY = {"luna": "Luna", "gemini": "Gemini", "claude": "Claude", "deepseek": "DeepSeek", "grok": "Grok"}
QUIET_TIMEOUT_S = 20
TIDY_AFTER_START_S = 5  # the core's manager starts after its interfaces; commands wait for it
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


def place_key(name: str) -> str:
    """A name as places compare them (contracts.PLACE_STATUS): its letters and digits in any script, lowercased."""
    return re.sub(r"[\W_]", "", (name or "").lower())[:40]


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
        # Who may be in a chat (`personas`, in order) and their names on the page (`classmates`, the same order).
        self.personas = [str(item) for item in config.get("personas") or list(DISPLAY)]
        shown = [str(item) for item in config.get("classmates") or []]
        self.classmates = [shown[index] if index < len(shown) else DISPLAY.get(name, name.capitalize())
                           for index, name in enumerate(self.personas)]
        self.roster = [{"name": name, "display": display} for name, display in zip(self.personas, self.classmates)]
        self.hostnames = {str(item).lower() for item in config.get("hostnames") or []}
        self.networks = [ipaddress.ip_network(str(item), strict=False) for item in config.get("networks") or []]
        # A reverse proxy on this machine (nginx) forwards the real client and scheme; only it is believed.
        self.proxies = [ipaddress.ip_network(str(item), strict=False)
                        for item in config.get("trusted_proxies") or ["127.0.0.1", "::1"]]
        # Clients beyond the local network, through that proxy only, and only with a passphrase set.
        self.remote_access = config.get("remote_access") is True
        upload_mb = float(config.get("max_upload_mb", 50))
        self.display = Display(markdown=True, edits=False, progress=True, quotes=False, files=True,
                               max_upload_mb=upload_mb, max_text_chars=int(config.get("max_text_chars", 20000)))
        self.port = None
        self.store: Store | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._progress: dict[str, dict] = {}  # run (or conversation) → the latest progress event
        self._failures: dict[str, deque] = {}
        self._refusals: deque = deque()  # when the guard last logged a refusal (rate limit)
        self._remote_failures: deque = deque()  # failed sign-ins from beyond the local network, all clients
        self._quiet: dict[str, dict] = {}  # request ID → {conversation, purpose, future}: the page's own commands
        self._setups: dict[str, asyncio.Future] = {}  # a new chat whose classmates are being set up
        self._background: set[asyncio.Task] = set()
        self._stopping = False
        self._runner: web.AppRunner | None = None
        self.address_shown = ""
        # The mini map: MomoChat and the other places interfaces publish in the core's directory (the Classroom next
        # door, say; contracts 1.7). `map: false` turns it off; with no other place there is simply no map.
        self.map = config.get("map", True) is not False
        self.places_poll_s = max(2.0, float(config.get("places_poll_s", 5)))
        self.places: dict | None = None

    # ---- lifecycle -------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        self.store = Store(self.data_dir / "webui.db")
        self.store.ensure(DEFAULT_CONVERSATION, DEFAULT_TITLE)
        # Whoever opens the page is the creator; approvals they start come back to the page. With no other
        # interface offering one, the classroom also becomes the control route.
        port.set_owners([self.user_id], control=Address(self.name, DEFAULT_CONVERSATION), fallback=True)
        app = web.Application(middlewares=[self._guard],
                              client_max_size=int(self.display.max_upload_mb * 1024 * 1024) + 65536)
        app.add_routes([
            web.get("/", self._index), web.get("/login", self._login_page), web.post("/login", self._login),
            web.get("/static/{name}", self._static), web.get("/avatars/{key}", self._avatar),
            web.get("/favicon.ico", self._favicon), web.get("/cafe", self._cafe),
            web.get("/api/cafe", self._cafe_state), web.get("/rooms/{name}", self._room),
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
        self._spawn(self._tidy(delay=TIDY_AFTER_START_S))
        if self.map and callable(getattr(port, "publish", None)):  # a core before 1.7 has no directory: no map
            port.publish(link=self.address_shown, status=self.place())
            self._spawn(self._watch_places())

    def place(self) -> dict:
        """MomoChat as a place (contracts.PLACE_STATUS): the students, for any interface's map. `views` are this
        page's entries in every page's places bar, which sorts all places' views by `order` (paths relative to the
        published link)."""
        return {"place": {"name": "MomoChat", "icon": "💬", "note": ""},
                "people": [{"key": place_key(item["name"]), "name": item["display"],
                            "avatar": avatar_key(item["name"])} for item in self.roster],
                "views": [{"name": "Cafe", "icon": "☕", "path": "cafe", "order": 10},
                          {"name": "MomoChat", "icon": "💬", "path": "", "order": 20}]}

    def here(self) -> dict:
        return {"interface": self.name, "link": self.address_shown, **self.place()}

    def _places(self) -> dict | None:
        """The classroom and the other places in the directory; None when no other place is published."""
        directory = self.port.directory() if self.port is not None else {}
        others = [{"interface": name, "link": entry.get("link", ""), **entry.get("status", {})}
                  for name, entry in directory.items()
                  if name != self.name and isinstance(entry.get("status"), dict) and entry["status"].get("place")]
        if not others:
            return None
        return {"here": {"interface": self.name, "link": self.address_shown, **self.place()}, "others": others}

    async def _watch_places(self) -> None:
        """While a page is open, read who is where from the directory, and tell the pages when it changes."""
        while not self._stopping:
            if self._sockets:
                try:
                    found = self._places()
                except Exception as error:  # the map is a nicety: it never stops the classroom
                    self.port.log("places_failed", error=type(error).__name__)
                    found = None
                if found != self.places:
                    self.places = found
                    await self._broadcast({"type": "places", "places": found})
            await asyncio.sleep(self.places_poll_s)

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def stop(self) -> None:
        self._stopping = True
        for quiet in self._quiet.values():  # nothing will answer them now; a chat being set up is finished by
            if not quiet["future"].done():  # _tidy at the next start
                quiet["future"].set_result({"ok": False, "code": "stopping", "text": "", "result": {}})
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
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
        client, scheme, proxied = self._client(request)
        if not local_remote(client, self.networks):
            if not (proxied and self.remote_access):
                return self._refuse(request, "network", "Only the local network may use this page.")
            if not self.passphrase:
                return self._refuse(request, "network", "Remote access needs a passphrase (WEBUI_PASSPHRASE).")
        if not local_host(request.headers.get("Host"), self.hostnames):
            return self._refuse(request, "host", "Open this page by the computer's address.")
        origin = request.headers.get("Origin")
        if origin is not None and origin != f"{scheme}://{request.headers.get('Host')}":
            # Another site in this browser trying to use the page (or its WebSocket) on the user's behalf.
            return self._refuse(request, "origin", "Cross-site requests are refused.")
        public = request.path in ("/login", "/static/style.css", "/favicon.ico")
        if self.passphrase and not public and not self._signed_in(request):
            if request.method == "GET" and request.path in ("/", "/cafe"):  # the pages: sign in first
                raise web.HTTPSeeOther("login")  # relative: also right under a proxy's path (/chat/login)
            return self._secured(web.Response(status=401, text="Sign in first."))
        return self._secured(await handler(request))

    def _client(self, request: web.Request) -> tuple[str, str, bool]:
        """(client address, scheme, through the proxy). From a trusted proxy (nginx on this machine) that forwards
        them, the client and scheme it reports (it must replace, not append to, X-Forwarded-For); otherwise the
        connection's own. Nobody else's forwarding headers are believed."""
        remote = request.remote or ""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded and self._trusted(remote):
            scheme = request.headers.get("X-Forwarded-Proto", "").strip().lower()
            return forwarded.split(",")[-1].strip(), scheme if scheme in ("http", "https") else request.scheme, True
        return remote, request.scheme, False

    def _trusted(self, remote: str) -> bool:
        try:
            address = ipaddress.ip_address(remote.split("%", 1)[0])
        except ValueError:
            return False
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        return any(address in network for network in self.proxies)

    def _refuse(self, request: web.Request, check: str, text: str) -> web.Response:
        """A 403 from the guard, logged (at most 20 a minute, so a hostile page cannot flood the log) with what
        the request showed, so a refusal can be told from a real attack afterwards."""
        now = time.monotonic()
        while self._refusals and now - self._refusals[0] > 60:
            self._refusals.popleft()
        if len(self._refusals) < 20:
            self._refusals.append(now)
            self.port.log("refused", check=check, method=request.method, path=request.path[:100],
                          remote=self._client(request)[0][:60], host=str(request.headers.get("Host") or "")[:100],
                          origin=str(request.headers.get("Origin") or "")[:100])
        return self._secured(web.Response(status=403, text=text))

    @staticmethod
    def _secured(response):
        if isinstance(response, web.WebSocketResponse):
            return response
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # same-origin, not no-referrer: under no-referrer a browser sends `Origin: null` with a form POST (the
        # sign-in form), which the guard above would refuse. Other sites still get no referrer from the page.
        response.headers.setdefault("Referrer-Policy", "same-origin")
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
            raise web.HTTPSeeOther("./")
        remote, scheme, _ = self._client(request)
        now = time.monotonic()
        # Per client: 5 wrong tries in 5 minutes. Beyond the local network also all clients together: 20 in 10
        # minutes (many addresses, one passphrase), which never locks out the home network.
        for key in [key for key, tries in self._failures.items() if not tries or now - tries[-1] > 300]:
            self._failures.pop(key)
        while len(self._failures) >= 1000:
            self._failures.pop(next(iter(self._failures)))
        failures = self._failures.setdefault(remote, deque(maxlen=10))
        while failures and now - failures[0] > 300:
            failures.popleft()
        while self._remote_failures and now - self._remote_failures[0] > 600:
            self._remote_failures.popleft()
        outside = not local_remote(remote, self.networks)
        if len(failures) >= 5 or (outside and len(self._remote_failures) >= 20):
            return await self._login_page(request, "Too many tries; wait a few minutes.")
        form = await request.post()
        if not hmac.compare_digest(str(form.get("passphrase", "")).encode(), self.passphrase.encode()):
            failures.append(now)
            if outside:
                self._remote_failures.append(now)
            await asyncio.sleep(1)
            self.port.log("login_failed", remote=remote[:60])
            return await self._login_page(request, "That passphrase is not right.")
        response = web.HTTPSeeOther("./")  # relative: back to the page, also under a proxy's path
        # Path "/" here; a proxy serving the page under a path narrows it (nginx: proxy_cookie_path).
        response.set_cookie(COOKIE, self._session_value(), max_age=30 * 86400, httponly=True, samesite="Strict",
                            path="/", secure=scheme == "https")
        raise response

    @staticmethod
    def _html(text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))

    # ---- pages and files ----------------------------------------------------------------------------
    async def _index(self, request: web.Request) -> web.Response:
        page = (self.folder / "static" / "index.html").read_text(encoding="utf-8")
        places = self._places() or {"here": self.here(), "others": []}  # for the places bar's first paint
        return web.Response(text=self._fill(page, title=self._html(self.title), places=self._json(places)),
                            content_type="text/html", headers={"Cache-Control": "no-cache"})

    @staticmethod
    def _json(value) -> str:
        """JSON to put inside a page's <script type="application/json">: it never ends the tag."""
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")

    @staticmethod
    def _fill(page: str, **values: str) -> str:
        """A page with its {{name}} holes filled, in one pass (a value never fills another's hole)."""
        return re.sub(r"\{\{(\w+)\}\}", lambda match: values.get(match.group(1), match.group(0)), page)

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

    # ---- the cafe: the students walking around a preset room; a tap opens her chat ------------------------
    async def _cafe(self, request: web.Request) -> web.Response:
        """The page with its data inside (`{{data}}`, JSON the script reads at once), so the room and the students
        are there on the first paint."""
        page = (self.folder / "static" / "cafe.html").read_text(encoding="utf-8")
        return web.Response(text=self._fill(page, title=self._html(self.title), data=self._json(self.cafe())),
                            content_type="text/html", headers={"Cache-Control": "no-cache"})

    def rooms(self) -> list[str]:
        """The preset room pictures in `rooms/` (beside the drawn ones the page has), by file name."""
        folder = self.folder / "rooms"
        if not folder.is_dir():
            return []
        return sorted(path.name for path in folder.iterdir()
                      if path.suffix.lower() in AVATAR_TYPES and ROOM_FILE.fullmatch(path.name.lower()) and path.is_file())

    async def _room(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name in self.rooms():
            path = self.folder / "rooms" / name
            return web.FileResponse(path, headers={"Content-Type": AVATAR_TYPES[path.suffix.lower()],
                                                   "Cache-Control": "max-age=300"})
        raise web.HTTPNotFound()

    def cafe(self) -> dict:
        """Who is in the cafe (every student: she is anywhere at once), whether she can answer yet (her model profile
        is named after her, as the room's are), the rooms, and the places for the places bar."""
        ready = getattr(self.port, "models_ready", None) if self.port is not None else None
        students = [{**item, "avatar": avatar_key(item["name"]),
                     "ready": bool(ready(item["name"])) if callable(ready) else True} for item in self.roster]
        return {"title": self.title, "students": students, "avatars": self.avatars(), "rooms": self.rooms(),
                "here": self.here(), "places": self._places()}

    async def _cafe_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.cafe(), headers={"Cache-Control": "no-cache"})

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
        limit = int(self.display.max_upload_mb * 1024 * 1024)
        total = 0  # streamed parts are not covered by aiohttp's client_max_size: count them here
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
                    total += len(chunk)
                    if total > limit:
                        break
                    handle.write(chunk)
            if total > limit:
                shutil.rmtree(target.parent, ignore_errors=True)
                for item in kept:  # the whole upload is refused, not half of it
                    self.store.delete_file(item["id"])
                    shutil.rmtree(self.data_dir / "uploads" / item["id"], ignore_errors=True)
                return web.json_response({"error": "too_large", "max_upload_mb": self.display.max_upload_mb},
                                         status=413)
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
        if not 0 <= after < 2 ** 63:  # a message ID: SQLite's integer range, never negative
            raise web.HTTPBadRequest()
        return web.json_response({"messages": self.store.messages(conversation, after)})

    # ---- the live connection --------------------------------------------------------------------------
    def state(self) -> dict:
        commands = [{"name": command.name, "description": command.description, "usage": command.usage(),
                     "examples": [example for example in command.examples if example]}
                    for command in self.port.commands()]
        return {"title": self.title, "user": {"id": self.user_id, "name": self.user_name},
                "classmates": self.classmates, "roster": self.roster, "avatars": self.avatars(), "commands": commands,
                "conversations": self.store.conversations(), "progress": list(self._progress.values()),
                "default": DEFAULT_CONVERSATION, "max_upload_mb": self.display.max_upload_mb, "here": self.here(),
                "places": self._places() if self.map and self.port is not None and
                callable(getattr(self.port, "directory", None)) else None}

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
                    try:
                        await self._handle(ws, event)
                    except Exception as error:  # one bad event never closes the page's connection
                        if self._stopping or ws.closed:
                            break
                        self.port.log("event_failed", event=str(event.get("type"))[:20], error=type(error).__name__)
        finally:
            self._sockets.discard(ws)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, event: dict) -> None:
        kind = event.get("type")
        if kind == "order":
            listed = event.get("conversations")
            if not isinstance(listed, list):
                return
            ids = [str(item) for item in listed[:500] if CONVERSATION_ID.fullmatch(str(item))]
            self.store.order(ids)
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
            return
        conversation = str(event.get("conversation") or "")
        if not CONVERSATION_ID.fullmatch(conversation):
            return
        if kind == "new":
            if self.store.conversation(conversation) is None:
                await self._new(ws, conversation, str(event.get("title") or "New chat")[:80], event.get("personas"))
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
            await self._ask_models(ws, conversation, "$model", "overview")
        elif kind == "model":  # a choice from that menu (or `reset`)
            persona, model = str(event.get("persona") or ""), str(event.get("model") or "")
            if PERSONA.fullmatch(persona) and MODEL.fullmatch(model):
                await self._ask_models(ws, conversation, f"$model {persona} {model}", "choose")

    async def _quiet_command(self, conversation: str, text: str, purpose: str) -> asyncio.Future:
        """A `$command` the page sends for itself: it is not shown in the chat, and its answer comes back as
        data instead of a bubble (the returned future: {ok, code, text, result}). The core still sees and records
        it like a typed command (the room leaves commands out of what its characters read)."""
        key = f"{QUIET_PREFIX}{uuid.uuid4().hex}"
        # Model-menu asks are bounded (a page can send them in a loop); a chat's setup never is evicted: its
        # answer must reach _answer_quietly to be cleaned up (and there is at most one per open page).
        asks = [item for item, quiet in self._quiet.items() if quiet["purpose"] in MODEL_PURPOSES]
        while purpose in MODEL_PURPOSES and len(asks) >= 100:
            lost = self._quiet.pop(asks.pop(0))["future"]
            if not lost.done():
                lost.set_result({"ok": False, "code": "lost", "text": "", "result": {}})
        future = asyncio.get_running_loop().create_future()
        self._quiet[key] = {"conversation": conversation, "purpose": purpose, "future": future}
        result = await self.port.submit(Request(
            id=key, address=Address(self.name, conversation), sender=Sender(self.user_id, self.user_name), text=text,
            received_at=time.time(), is_direct=True, mentions_me=True, display=self.display))
        if not result.accepted:
            self._quiet.pop(key, None)
            if not future.done():
                future.set_result({"ok": False, "code": result.reason, "text": "", "result": {}, "refused": True})
        return future

    async def _quietly(self, conversation: str, text: str, purpose: str) -> dict:
        """A quiet command, waiting for its answer."""
        future = await self._quiet_command(conversation, text, purpose)
        try:
            return await asyncio.wait_for(asyncio.shield(future), QUIET_TIMEOUT_S)
        except asyncio.TimeoutError:
            answer = {"ok": False, "code": "timeout", "text": "", "result": {}}
            if not future.done():
                future.set_result(answer)  # given up: an answer that still comes is a late one
            return answer

    async def _ask_models(self, ws: web.WebSocketResponse, conversation: str, text: str, purpose: str) -> None:
        """The model menu's `$model`: its answer is broadcast as a `models` event when it comes; a refusal is told
        to this page at once."""
        future = await self._quiet_command(conversation, text, purpose)
        if future.done() and future.result().get("refused"):
            answer = {key: value for key, value in future.result().items() if key != "refused"}
            await ws.send_json({"type": "models", "conversation": conversation, "purpose": purpose, **answer})

    # ---- who is in a chat -----------------------------------------------------------------------------
    async def _new(self, ws: web.WebSocketResponse, conversation: str, title: str, chosen) -> None:
        """A new chat, with the whole class (`chosen` absent) or the classmates picked for it; one classmate
        makes a direct chat with her. A chat whose classmates differ from the web UI's assignment gets its own
        (`core.set_assignment`, asked quietly as the creator); messages sent to it meanwhile wait for that."""
        if chosen is None:
            self.store.ensure(conversation, title)
            return
        picked = {str(item) for item in chosen[:50]} if isinstance(chosen, list) else set()
        personas = [name for name in self.personas if name in picked]
        if not personas or len(personas) != len(picked):
            await ws.send_json({"type": "error", "text": "Pick classmates from the list."})
            return
        self.store.ensure(conversation, title, personas)
        await self._broadcast({"type": "conversations", "items": self.store.conversations()})
        # Messages sent to it meanwhile learn the outcome only once a failed chat is gone again: they must
        # never reach the chat as it is before its assignment (the whole class).
        setup = asyncio.get_running_loop().create_future()
        self._setups[conversation] = setup
        ready = False
        try:
            problem = await self._assign(conversation, personas)
            if self._stopping:
                return  # the chat stays; _tidy finishes its setup at the next start
            if self.store.conversation(conversation) is None:
                await self._unassign(conversation)  # deleted (on any page) while it was being set up
                return
            if problem:
                await self._delete(conversation)
                await ws.send_json({"type": "error", "text": f"Couldn't start that chat: {problem}"})
            ready = not problem
        finally:
            self._setups.pop(conversation, None)
            setup.set_result(ready)

    async def _assign(self, conversation: str, personas: list[str]) -> str:
        """Give one chat these personas, keeping everything else of the assignment it has now. Returns what went
        wrong, or "" (checked against the core's policy, not the command's wording)."""
        address = Address(self.name, conversation)
        base = self.port.policy(address).get("assignment")
        if base is None:
            return "no engine is assigned to the web UI's chats"
        if list(base["personas"]) == personas:
            if base.get("pattern") != str(address):
                self.store.unmark_assigned(conversation)  # nothing of its own to keep, or to remove later
            return ""
        value = {key: item for key, item in base.items() if key not in ("pattern", "since")}
        arguments = {"pattern": str(address), "assignment": {**value, "personas": personas}}
        # Recorded first: if the answer comes too late (the chat is deleted as failed by then) or never, the
        # assignment may still exist, and _tidy removes it.
        self.store.mark_assigned(conversation)
        answer = await self._quietly(conversation, "$call core.set_assignment "
                                     + json.dumps(arguments, ensure_ascii=False), "assign")
        now = self.port.policy(address).get("assignment") or {}
        if now.get("pattern") == str(address) and list(now.get("personas") or []) == personas:
            self.port.log("chat_assigned", personas=len(personas))
            return ""
        return answer.get("text") or answer.get("code") or "the core did not take it"

    async def _delete(self, conversation: str) -> None:
        """Remove a chat from the page: its open work is cancelled, its transcript and files are deleted, and
        its own assignment (from the new-chat picker) is removed. (The core keeps its own history of the
        conversation, as for any other interface.)"""
        chat = self.store.conversation(conversation)
        if chat is None:
            return
        address = Address(self.name, conversation)
        self._adopt(chat)
        await self.port.cancel(address, "chat deleted in the web UI")
        kept = {(self.data_dir / "files").resolve(), (self.data_dir / "uploads").resolve()}
        for path in self.store.delete(conversation):
            if path.resolve().parent.parent in kept:  # only the file's own folder under files/ or uploads/
                shutil.rmtree(path.parent, ignore_errors=True)
        for key in [key for key, event in self._progress.items() if event["conversation"] == conversation]:
            self._progress.pop(key, None)
        if not self.store.conversations():
            self.store.ensure(DEFAULT_CONVERSATION, DEFAULT_TITLE)
        self.port.log("chat_deleted")
        await self._broadcast({"type": "deleted", "conversation": conversation,
                               "items": self.store.conversations()})
        await self._unassign(conversation)

    async def _unassign(self, conversation: str) -> None:
        """Remove the runtime assignment this page set for a chat that is gone. Only ours (recorded in
        `assigned`): one configured in core.yml, or set by someone else, is never touched. The record stays
        while its `set_assignment` may still land (asked, not yet answered)."""
        if conversation not in self.store.assigned():
            return
        address = Address(self.name, conversation)
        if (self.port.policy(address).get("assignment") or {}).get("pattern") == str(address):
            await self._quietly(conversation, "$call core.delete_assignment "
                                + json.dumps({"pattern": str(address)}), "unassign")
        asking = any(item["purpose"] == "assign" and item["conversation"] == conversation
                     for item in self._quiet.values())
        if not asking and (self.port.policy(address).get("assignment") or {}).get("pattern") != str(address):
            self.store.unmark_assigned(conversation)
            self.port.log("chat_unassigned")

    def _adopt(self, chat: dict) -> None:
        """Record a picker chat's own assignment that predates the `assigned` record (an older version made it):
        only the picker gives a chat personas, so an assignment for exactly that chat is this page's."""
        address = Address(self.name, chat["id"])
        if chat.get("personas") and (self.port.policy(address).get("assignment") or {}).get("pattern") == str(address):
            self.store.mark_assigned(chat["id"])

    async def _repair(self, conversation: str, personas: list[str]) -> str:
        """Give a chat its classmates again if the core's assignment no longer matches (a setup cut off by a
        crash or restart). One at a time per chat: a send and `_tidy` may both find it. Returns what went wrong."""
        while (setup := self._setups.get(conversation)) is not None:
            await asyncio.wait({setup}, timeout=QUIET_TIMEOUT_S + 5)
            if not setup.done():
                return "still being set up"
        now = self.port.policy(Address(self.name, conversation)).get("assignment") or {}
        if list(now.get("personas") or []) == personas:
            return ""
        setup = asyncio.get_running_loop().create_future()
        self._setups[conversation] = setup
        try:
            problem = await self._assign(conversation, personas)
            if self.store.conversation(conversation) is None:  # deleted (on any page) while it was repaired
                await self._unassign(conversation)
                problem = "deleted"
        finally:
            self._setups.pop(conversation, None)
            setup.set_result(True)
        self.port.log("chat_reassigned", ok=not problem)
        return problem

    async def _tidy(self, delay: float = 0) -> None:
        """Make the core's assignments match the page's chats again: remove those left behind by chats that are
        gone (a setup answered after its chat was deleted as failed, a deletion cut off by a restart), and set
        again those a chat with some of the classmates lacks (a setup cut off by a restart or a crash)."""
        if delay:
            await asyncio.sleep(delay)
        picked = {chat["id"]: chat["personas"] for chat in self.store.conversations() if chat["personas"]}
        for conversation in sorted(set(self.store.assigned()) | set(picked)):
            if self.store.conversation(conversation) is None:
                await self._unassign(conversation)
                continue
            personas = picked.get(conversation)
            if personas:
                self._adopt(self.store.conversation(conversation))
                await self._repair(conversation, personas)

    async def _send(self, ws: web.WebSocketResponse, conversation: str, event: dict) -> None:
        request_id = str(event.get("id") or "")[:64] or uuid.uuid4().hex
        text = str(event.get("text") or "")[: self.display.max_text_chars]
        files = []
        listed = event.get("files")
        for file_id in (listed if isinstance(listed, list) else [])[:10]:
            record = self.store.file(str(file_id))
            if record is not None:
                files.append(record)
        if not text.strip() and not files:
            return
        setup = self._setups.get(conversation)
        if setup is not None:  # its own assignment first, or the message would go to the whole class
            await asyncio.wait({setup}, timeout=QUIET_TIMEOUT_S + 5)
            if not setup.done() or not setup.result():
                await ws.send_json({"type": "ack", "id": request_id, "ok": False, "reason": "chat_not_ready"})
                return
        record = self.store.conversation(conversation)
        if record is None:  # deleted (here, on another device, or after its setup failed): never re-created by a
            # message, which would bring it back with the whole class instead of the classmates it was for
            await ws.send_json({"type": "ack", "id": request_id, "ok": False, "reason": "unknown_chat"})
            return
        if record["personas"] and await self._repair(conversation, record["personas"]):
            # Its own assignment is missing (a crash cut its setup off) and could not be set again: the message
            # would reach the whole class instead. Or the chat was deleted meanwhile (checked below).
            gone = self.store.conversation(conversation) is None
            await ws.send_json({"type": "ack", "id": request_id, "ok": False,
                                "reason": "unknown_chat" if gone else "chat_not_ready"})
            return
        if self.store.conversation(conversation) is None:  # deleted while this message waited (for a repair
            await ws.send_json({"type": "ack", "id": request_id, "ok": False, "reason": "unknown_chat"})
            return  # another page ran): sending now would bring it back
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
        answer = {"ok": reply.kind == "text", "code": reply.code or "", "text": reply.text,
                  "result": result if isinstance(result, dict) else {}}
        if not quiet["future"].done():
            quiet["future"].set_result(answer)
        elif quiet["purpose"] == "assign" and self.store.conversation(quiet["conversation"]) is None:
            self._spawn(self._unassign(quiet["conversation"]))  # it came too late: its chat was given up
        if quiet["purpose"] in MODEL_PURPOSES:
            await self._broadcast({"type": "models", "conversation": quiet["conversation"],
                                   "purpose": quiet["purpose"], **answer})
        return Delivery(True, message_id=f"quiet-{reply.id}")

    async def deliver(self, reply: Reply) -> Delivery:
        if self.store is None:
            return Delivery(False, detail="not_started")
        quiet = self._quiet.get(reply.in_reply_to or "")
        if quiet is not None:  # (before the chat is re-created: removing a deleted chat's assignment answers too)
            return await self._answer_quietly(reply, quiet)
        if (reply.in_reply_to or "").startswith(QUIET_PREFIX):
            # The answer to a page's own command that is no longer awaited (dropped, or from before a restart):
            # never a bubble in the chat, and never a reason to bring a deleted chat back.
            return Delivery(True, message_id=f"quiet-{reply.id}")
        conversation = reply.address.conversation
        if not CONVERSATION_ID.fullmatch(conversation):
            conversation = DEFAULT_CONVERSATION
        if self.store.conversation(conversation) is None:
            self.store.ensure(conversation, "Notices" if conversation.startswith("_") else conversation)
            await self._broadcast({"type": "conversations", "items": self.store.conversations()})
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

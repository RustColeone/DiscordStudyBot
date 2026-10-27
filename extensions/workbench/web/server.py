"""The lab's interface: an HTTP and websocket API (the lab protocol, see PROTOCOL.md) and, optionally, the web page
that uses it. `club` and any other frontend use the same API, so none of them is special.

It is an ordinary interface (`workbench`, addresses `workbench:<task>`): what you type goes to the core through
`port.submit`, and what the club says comes back through `deliver`, as for Discord or the web UI. The rest (each
task's steps, its diff, the approval it waits for and what can be done now) comes from the module's views
(views.py), so every frontend shows the same rules instead of deciding them. It publishes the lab as a place in
the core's directory (contracts 1.7) and reads the other places from there, for the mini map.

Whoever opens it acts as the creator. It answers only private-network clients and refuses other websites
(Host and Origin checks, as the web UI does). `web: false` keeps the API and drops the page.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import re
import shutil
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from contracts import Address, Delivery, Display, Interface, Reply, Request, Sender

from .. import places, views
from ..kernel.flow import new_id
from ..workflows import catalog
from ..workspace.tools import ToolError, inside

STATIC = re.compile(r"^[a-z0-9_-]+\.(js|css|png|svg)$")
AVATAR = re.compile(r"^[a-z0-9_-]{1,40}$")
TASK_ID = re.compile(r"^t-[a-z0-9]{4,40}$")
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")
OUTBOX_LIMIT = 2000  # messages waiting for one page before it is taken as gone


class WorkbenchUI(Interface):
    def __init__(self, name: str, config: Mapping[str, Any], board, registry, trees, team, data_dir: Path,
                 avatars: list[Path], prices=None, workbench=None):
        self.name = name
        self.prices = prices
        self.workbench = workbench  # the capabilities' own rules (ticks), so the page and `$check` agree
        self.page = config.get("web", True) is not False
        self.config, self.board, self.registry, self.trees, self.team = config, board, registry, trees, team
        self.data_dir = Path(data_dir)
        self.files_dir = self.data_dir / "files"
        self.avatar_dirs = [path for path in avatars if path.is_dir()]
        self.display = Display(markdown=True, edits=False, progress=True, quotes=False, files=True,
                               max_upload_mb=float(config.get("max_upload_mb", 50)),
                               max_text_chars=int(config.get("max_text_chars", 40000)))
        self.user_id = str(config.get("user_id") or "creator")
        self.user_name = str(config.get("user_name") or _login_name())
        self.port_number = int(config.get("port", 4157))
        self.host = str(config.get("host") or "0.0.0.0")
        self.hostnames = {str(item).lower() for item in config.get("hostnames") or ()}
        self.static = Path(__file__).resolve().parent / "static"
        self.sockets: set[web.WebSocketResponse] = set()
        self.outboxes: dict[web.WebSocketResponse, asyncio.Queue] = {}  # each page's messages, in order
        self._publishing: asyncio.TimerHandle | None = None
        self.progress: dict[str, dict[str, dict]] = {}
        self.port = None
        self.runner: web.AppRunner | None = None
        self.url = ""
        self._unsubscribe = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watcher: asyncio.Task | None = None
        self._place: dict = {}
        self._places: dict = {}

    # ---- lifecycle ------------------------------------------------------------------------------
    async def start(self, port) -> None:
        self.port = port
        port.set_owners([self.user_id])  # whoever opens the page is the creator (unless core.yml names owners)
        self._loop = asyncio.get_running_loop()
        app = web.Application(middlewares=[self._guard], client_max_size=4 * 1024 * 1024)
        app.add_routes([  # the lab protocol (PROTOCOL.md)
            web.get("/api/tasks/{task}", self._task_api), web.get("/api/tasks/{task}/diff", self._diff_api),
            web.get("/api/tasks/{task}/file", self._file_api), web.get("/files/{file}/{name}", self._file),
            web.get("/api/prices", self._prices_api), web.get("/api/presence", self._presence_api),
            web.get("/api/state", self._state_api), web.get("/ws", self._socket),
            web.get("/avatars/{key}", self._avatar), web.get("/favicon.ico", self._favicon)])
        if self.page:  # the web page, one frontend of the API
            app.add_routes([web.get("/", self._index), web.get("/static/{name}", self._static)])
        else:
            app.add_routes([web.get("/", self._no_page)])
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, self.host, self.port_number)
            await site.start()
        except OSError as error:
            port.set_ready(False, f"cannot listen on {self.host}:{self.port_number}: {error}")
            await self.runner.cleanup()
            raise
        bound = site._server.sockets[0].getsockname()[1] if site._server and site._server.sockets else self.port_number
        self.port_number = bound
        self.url = f"http://{_lan_address() if self.host in ('0.0.0.0', '::') else self.host}:{bound}"
        print(f"[{self.name}] Open {self.url} for the club's workbench." if self.page else
              f"[{self.name}] The lab's API is at {self.url} (the page is off).", flush=True)
        self._unsubscribe = self.board.subscribe(self._on_board)
        self._publish()
        self._watcher = asyncio.create_task(self._watch_places())

    def link(self, task: str, tab: str = "changes") -> str:
        """Where a task opens in this page (its changes, for a message in Discord, say); empty until it listens."""
        base = str(self.config.get("public_url") or self.url).rstrip("/")
        return f"{base}/#{task}{'/' + tab if tab else ''}" if base else ""

    async def stop(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
        if self._publishing is not None:
            self._publishing.cancel()
        if self._unsubscribe:
            self._unsubscribe()
        for page in list(self.sockets):
            self._drop(page)
            await _close(page)
        if self.runner is not None:
            await self.runner.cleanup()
        self.board.close()  # the module stops with its page

    # ---- the guard --------------------------------------------------------------------------------
    @web.middleware
    async def _guard(self, request: web.Request, handler):
        peer = (request.transport.get_extra_info("peername") or ("", 0))[0] if request.transport else ""
        if not _private(peer):
            return web.Response(status=403, text="This page answers only the local network.")
        host = (urlsplit(f"//{request.host or ''}").hostname or "").lower()  # (an IPv6 host too: [::1]:4157)
        if not (_is_ip(host) or host == "localhost" or host.endswith(LOCAL_SUFFIXES) or host in self.hostnames
                or host == socket.gethostname().lower()):
            return web.Response(status=403, text="Unknown host name (add it to hostnames in the config).")
        origin = request.headers.get("Origin")
        public = urlsplit(str(self.config.get("public_url") or ""))  # behind an HTTPS proxy, the page's own origin
        allowed = {f"{request.scheme}://{request.host}", *([f"{public.scheme}://{public.netloc}"] if public.netloc
                                                          else [])}
        if origin and origin.rstrip("/") not in allowed:
            return web.Response(status=403, text="Requests from other sites are refused.")
        response = await handler(request)
        if isinstance(response, web.StreamResponse) and not isinstance(response, web.WebSocketResponse):
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Content-Security-Policy",
                                        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                        "connect-src 'self'; frame-ancestors 'none'; object-src 'none'")
        return response

    # ---- places: the lab in the core's directory, and the others for the map ------------------------------
    def presence(self) -> dict:
        """The lab as a place (places.py), as published."""
        return places.lab_place(self.board, self.team, self.link, float(self.config.get("visit_minutes", 30)))

    def _publish(self) -> None:
        """Tell the other interfaces where the lab is and who is in it (contracts 1.7), when that changed."""
        if self.port is None:
            return
        place = self.presence()
        if place != self._place:
            self._place = place
            self.port.publish(link=self.url, status=place)

    def places(self) -> dict:
        """This place and the others that published one (the classroom, say), for the mini map."""
        directory = self.port.directory() if self.port is not None else {}
        others = [{"interface": name, "link": entry.get("link", ""), **entry.get("status", {})}
                  for name, entry in directory.items()
                  if name != self.name and isinstance(entry.get("status"), dict) and entry["status"].get("place")]
        return {"here": {"interface": self.name, "link": self.url, **(self._place or self.presence())},
                "others": others}

    def links(self) -> dict[str, str]:
        """Where each interface is reached (for "open the classroom"); classroom_url still wins for its own."""
        found = {name: entry.get("link", "") for name, entry in (self.port.directory() if self.port else {}).items()}
        if self.config.get("classroom_url"):
            found["webui"] = str(self.config["classroom_url"])
        return {name: link for name, link in found.items() if link}

    async def _watch_places(self) -> None:
        """Every few seconds: publish the lab when it changed (a finished visit ends with no event to say so), and
        tell open pages when any place changed."""
        every = max(2.0, float(self.config.get("places_poll_s", 5)))
        while True:
            try:
                self._publish()
                current = {"places": self.places(), "links": self.links()}
                if current != self._places:
                    self._places = current
                    self._broadcast({"type": "places", **current})
            except asyncio.CancelledError:
                raise
            except Exception as error:  # the map is a nicety: it never stops the lab
                self.port.log("places_failed", error=type(error).__name__)
            await asyncio.sleep(every)

    # ---- pages and files --------------------------------------------------------------------------
    async def _no_page(self, request):
        return web.Response(text="The lab's API (see PROTOCOL.md); the web page is turned off (web: false).")

    async def _state_api(self, request):
        return web.json_response(self._state())

    async def _index(self, request):
        html = (self.static / "index.html").read_text(encoding="utf-8")
        title = str(self.config.get("title") or "Club Room")
        # The page comes with what its first hello would say, so the sessions are there on the first paint.
        state = json.dumps(self._state(), ensure_ascii=False).replace("<", "\\u003c")  # (never ends the tag)
        html = re.sub(r"\{\{(title|state)\}\}",
                      lambda match: _escape(title) if match.group(1) == "title" else state, html)  # in one pass
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def _static(self, request):
        name = request.match_info["name"]
        path = self.static / name
        if not STATIC.match(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    async def _favicon(self, request):
        for directory in (self.static, *self.avatar_dirs):
            path = directory / "favicon.png"
            if path.is_file():
                return web.FileResponse(path)
        raise web.HTTPNotFound()

    async def _avatar(self, request):
        key = request.match_info["key"]
        if not AVATAR.match(key):
            raise web.HTTPNotFound()
        for directory in self.avatar_dirs:
            for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                path = directory / f"{key}{suffix}"
                if path.is_file():
                    return web.FileResponse(path, headers={"Cache-Control": "max-age=3600"})
        raise web.HTTPNotFound()

    async def _file(self, request):
        file_id, name = request.match_info["file"], request.match_info["name"]
        if not re.match(r"^[a-f0-9]{32}$", file_id):
            raise web.HTTPNotFound()
        path = self.files_dir / file_id / name
        if not path.is_file() or path.parent.parent != self.files_dir:
            raise web.HTTPNotFound()
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = mime.startswith(("image/", "audio/", "video/")) or mime == "application/pdf"
        headers = {"Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{name}"}
        if not inline:
            headers["Content-Type"] = "text/plain; charset=utf-8" if name.endswith(".diff") else "application/octet-stream"
        return web.FileResponse(path, headers=headers)

    def _task_or_404(self, request) -> dict:
        task_id = request.match_info["task"]
        task = self.board.task(task_id) if TASK_ID.match(task_id) else None
        if task is None:
            raise web.HTTPNotFound()
        return task

    async def _task_api(self, request):
        task = self._task_or_404(request)
        return web.json_response({"task": self._task_view(task), "steps": self.board.steps(task["id"]),
                                  "messages": self.board.messages(task["id"])})

    async def _diff_api(self, request):
        """The task's change set: live from its worktree, or the patch kept when it landed or was discarded."""
        task = self._task_or_404(request)
        data = task["data"]
        project = self.registry.get(task["project"])
        worktree = data.get("worktree")
        if worktree and Path(worktree).is_dir() and project is not None:
            change = await self.trees.change(Path(worktree), data["base"], project.link)
            return web.json_response({**change.to_dict(patch=True), "live": True})
        kept = self.trees.root.parent / "patches" / f"{task['id']}.diff"
        patch = kept.read_text(encoding="utf-8") if kept.is_file() else ""
        change = data.get("change") or {}
        return web.json_response({"files": change.get("files", []), "stat": change.get("stat", "no changes"),
                                  "fingerprint": change.get("fingerprint", ""), "patch": patch, "live": False})

    async def _presence_api(self, request):
        return web.json_response(self.presence())

    async def _prices_api(self, request):
        """Where prices come from and each classmate's model's price; `?refresh=1` fetches when a day old."""
        if self.prices is None:
            return web.json_response({"error": "no price list"})
        if request.query.get("refresh"):
            await self.prices.refresh_if_stale()
        return web.json_response(self.prices.status(self.team))

    async def _file_api(self, request):
        """One file of the task as it is now (its worktree, else the checkout), for reading it whole."""
        task = self._task_or_404(request)
        project = self.registry.get(task["project"])
        if project is None:
            raise web.HTTPNotFound()
        worktree = task["data"].get("worktree")
        root = Path(worktree) if worktree and Path(worktree).is_dir() else project.path
        try:
            path = inside(root, request.query.get("path", ""), project.link)
        except ToolError:
            raise web.HTTPForbidden()
        if not path.is_file() or path.stat().st_size > 2_000_000:
            raise web.HTTPNotFound()
        return web.json_response({"path": request.query.get("path"), "text": path.read_text("utf-8", "replace")})

    # ---- the websocket ------------------------------------------------------------------------------
    async def _socket(self, request):
        socket_ = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await socket_.prepare(request)
        self.sockets.add(socket_)
        box = self.outboxes[socket_] = asyncio.Queue()
        writer = asyncio.ensure_future(self._writer(socket_, box))
        try:
            self._tell(socket_, {"type": "hello", "state": self._state()})
            async for message in socket_:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    event = json.loads(message.data)
                    await self._event(socket_, event)
                except Exception as error:  # one bad event never closes the page
                    self.port.log("event_failed", error=type(error).__name__)
                    self._tell(socket_, {"type": "error", "text": f"{type(error).__name__}: {error}"[:300]})
        finally:
            self._drop(socket_)
            writer.cancel()
        return socket_

    def _tell(self, page, message: dict) -> None:
        """Send `message` to one page after everything told it before (one queue and one writer per page: a slow
        frame never lets a later one overtake it). A page that falls far behind is closed; it reconnects and loads
        the state afresh."""
        box = self.outboxes.get(page)
        if box is None:
            return
        if box.qsize() >= OUTBOX_LIMIT:
            self._drop(page)
            return
        box.put_nowait(message)

    async def _writer(self, page, box: asyncio.Queue) -> None:
        while True:
            message = await box.get()
            if message is None or not await _send(page, message):
                self._drop(page)
                return

    def _drop(self, page) -> None:
        self.sockets.discard(page)
        box = self.outboxes.pop(page, None)
        if box is not None:
            box.put_nowait(None)
            if not page.closed:
                asyncio.ensure_future(_close(page))

    async def _event(self, socket_, event: dict) -> None:
        kind = event.get("type")
        if kind == "start":
            await self._start_task(socket_, event)
        elif kind == "send":
            await self._send_text(socket_, str(event.get("task", "")), str(event.get("text", "")),
                                  str(event.get("id") or uuid.uuid4().hex), str(event.get("action") or ""))
        elif kind == "load":  # a task's steps and messages, in order with the events (none lost or applied twice)
            task = self.board.task(str(event.get("task", "")))
            if task is not None:
                self._tell(socket_, {"type": "loaded", "task": self._task_view(task),
                                     "steps": self.board.steps(task["id"]), "messages": self.board.messages(task["id"])})
            else:
                self._tell(socket_, {"type": "loaded", "id": str(event.get("task", "")), "missing": True})
        elif kind == "tick":  # the same ticks as `$check`: one rule, kept by the capabilities
            task = self.board.task(str(event.get("task", "")))
            if task and self.workbench is not None:
                self.workbench.tick(task, [str(event.get("item", ""))], bool(event.get("checked")),
                                    str(event.get("fingerprint") or ""))
        elif kind == "add_project":
            project = self.registry.add(str(event.get("name", "")), str(event.get("path", "")),
                                        str(event.get("test") or "") or None)
            self._tell(socket_, {"type": "toast", "text": f"Added {project.name}"})
        elif kind == "create_project":
            project = self.registry.create(str(event.get("name", "")), str(event.get("path") or "") or None)
            self._tell(socket_, {"type": "toast", "text": f"Created {project.name} at {project.path}"})
        elif kind == "remove_project":
            self.board.remove_project(str(event.get("name", "")))
        elif kind == "forget":
            task = self.board.task(str(event.get("task", "")))
            if task and task["status"] in ("landed", "discarded", "answered", "failed", "cancelled") and not \
                    task["data"].get("worktree"):
                self.board.delete_task(task["id"])
        elif kind == "accept":  # following an invitation from the classroom: start it as a task
            await self._accept(socket_, event)
        elif kind == "report":  # take the result back to where the task came from
            await self._report(socket_, str(event.get("task", "")))
        elif kind == "folder":
            self._tell(socket_, {"type": "folder", "path": str(self.registry.default_folder(
                str(event.get("name") or "new-project")))})

    async def _start_task(self, socket_, event: dict) -> None:
        text = str(event.get("text") or "").strip()[: self.display.max_text_chars]
        project = str(event.get("project") or "")
        workflow = str(event.get("workflow") or "auto")
        client_id = str(event.get("id") or uuid.uuid4().hex)
        if not text or project not in self.registry.all() or workflow not in self._workflows():
            self._tell(socket_, {"type": "ack", "id": client_id, "ok": False, "reason": "invalid"})
            return
        task_id = new_id()
        title = text.splitlines()[0][:80]
        crew = self._crew(event.get("crew"))
        self.board.create_task(task_id, str(Address(self.name, task_id)), project, title, request=text,
                               by=self.user_name, origin=self.name, **({"crew": crew} if crew else {}))
        extra = {"task": task_id, "project": project}
        if workflow != "auto":
            extra["workflow"] = workflow
        result = await self._submit(task_id, client_id, text, extra)
        if not result:
            self.board.delete_task(task_id)
        self._tell(socket_, {"type": "ack", "id": client_id, "ok": bool(result), "reason": result.reason,
                              "task": task_id if result else None})

    async def _accept(self, socket_, event: dict) -> None:
        task = self.board.task(str(event.get("task", "")))
        client_id = str(event.get("id") or uuid.uuid4().hex)
        text = str(event.get("text") or "").strip()[: self.display.max_text_chars]
        project, workflow = str(event.get("project") or ""), str(event.get("workflow") or "auto")
        if task is None or task["status"] != "invited" or not text or project not in self.registry.all() \
                or workflow not in self._workflows():
            self._tell(socket_, {"type": "ack", "id": client_id, "ok": False, "reason": "invalid"})
            return
        if "crew" in event:  # who comes along, as picked on the arrival card (her suggestion by default)
            self.board.update_task(task["id"], data={"crew": self._crew(event.get("crew"))})
        self.board.update_task(task["id"], status="queued")  # taken: a second accept (another page, club) is refused
        extra = {"task": task["id"], "project": project, **({"workflow": workflow} if workflow != "auto" else {})}
        result = await self._submit(task["id"], client_id, text, extra)
        if not result:
            self.board.update_task(task["id"], status="invited")
        self._tell(socket_, {"type": "ack", "id": client_id, "ok": bool(result), "reason": result.reason})

    @staticmethod
    def _workflows() -> set[str]:
        return {item["name"] for item in catalog()}

    def _crew(self, names) -> list[str]:
        """Seats picked to come along (known ones, at most six)."""
        picked = [str(name) for name in names] if isinstance(names, list) else []
        return [name for name in dict.fromkeys(picked) if name in self.team.seats][:6]

    def report_text(self, task: dict) -> str:
        """The lab report that goes back to the conversation a task came from (views.py)."""
        return views.report_text(self.board, self.team, task, self.link)

    async def _report(self, socket_, task_id: str) -> None:
        task = self.board.task(task_id) if TASK_ID.match(task_id) else None
        origin = (task or {}).get("data", {}).get("origin_address") if task else None
        if not task or not origin:
            self._tell(socket_, {"type": "toast", "text": "This task has no conversation to report back to."})
            return
        if not views.can_report(task):
            self._tell(socket_, {"type": "toast", "text": "This task's report is on its way, or sent already."})
            return
        arguments = json.dumps({"address": origin, "text": self.report_text(task)}, ensure_ascii=False)
        client_id = f"report-{uuid.uuid4().hex}"
        result = await self._submit(task_id, client_id, f"$call core.send_message {arguments}", {"task": task_id},
                                    action="Report back")
        if result:  # sent once the core says it was delivered (deliver(), below)
            self.board.update_task(task_id, data={"reporting": {"at": time.time(), "to": origin, "status": task["status"],
                                                                "request": f"wb-{client_id}"}})

    async def _send_text(self, socket_, task_id: str, text: str, client_id: str, action: str) -> None:
        task = self.board.task(task_id) if TASK_ID.match(task_id) else None
        text = text.strip()[: self.display.max_text_chars]
        if task is None or not text:
            self._tell(socket_, {"type": "ack", "id": client_id, "ok": False, "reason": "unknown_task"})
            return
        if not task["address"].startswith(self.name + ":"):  # its conversation is elsewhere (it answers there)
            self._tell(socket_, {"type": "ack", "id": client_id, "ok": False, "reason": "not_here"})
            return
        result = await self._submit(task_id, client_id, text, {"task": task_id}, action)
        self._tell(socket_, {"type": "ack", "id": client_id, "ok": bool(result), "reason": result.reason})

    async def _submit(self, task_id: str, client_id: str, text: str, extra: dict, action: str = ""):
        request = Request(id=f"wb-{client_id}", address=Address(self.name, task_id),
                          sender=Sender(self.user_id, self.user_name), text=text, received_at=time.time(),
                          is_direct=True, mentions_me=True, display=self.display, extra=extra)
        result = await self.port.submit(request)
        if result and result.reason != "duplicate":
            item = ({"kind": "choice", "text": action, "command": text} if action else {"kind": "mine", "text": text})
            self.board.add_message(task_id, {**item, "at": time.time(), "speaker": self.user_name})
            self._mark_decision(task_id, text)  # (pressed, or typed into the box)
        return result

    def _mark_decision(self, task_id: str, command: str) -> None:
        """A pressed (or typed) $approve/$deny closes its approval card on every open page."""
        match = re.match(r"^\$(approve|deny)\s+([a-f0-9]{6})", command.strip())
        if not match:
            return
        decision, approval = match.groups()
        for item in self.board.messages(task_id):
            if item.get("code") == "approval_request" and item.get("data", {}).get("approval_id") == approval:
                self.board.update_message(item["id"], {**item, "decided": "approved" if decision == "approve" else
                                                       "denied"})

    # ---- deliveries ---------------------------------------------------------------------------------
    async def deliver(self, reply: Reply) -> Delivery:
        task_id = reply.address.conversation
        if reply.kind == "progress":
            if reply.run_id:
                entry = {"text": reply.text, "run_id": reply.run_id, "at": time.time(),
                         "actions": [action.__dict__ for action in reply.actions]}
                self.progress.setdefault(task_id, {})[reply.run_id] = entry
                self._broadcast({"type": "progress", "task": task_id, **entry})
            return Delivery(True, detail="shown")
        if reply.run_id and self.progress.get(task_id, {}).pop(reply.run_id, None) is not None:
            self._broadcast({"type": "progress_clear", "task": task_id, "run_id": reply.run_id})
        item: dict[str, Any] = {"kind": reply.kind, "text": reply.text, "speaker": reply.speaker, "label": reply.label,
                                "code": reply.code or "", "data": dict(reply.data), "run_id": reply.run_id or "",
                                "source": reply.source, "at": time.time(),
                                "actions": [action.__dict__ for action in reply.actions]}
        if reply.kind == "file" and reply.file is not None:
            source = Path(reply.file.path)
            if not source.is_file():
                return Delivery(False, detail="missing_file")
            file_id = uuid.uuid4().hex
            name = re.sub(r"[^\w.\- ]", "_", reply.file.name or source.name)[:120] or "file"
            target = self.files_dir / file_id / name
            target.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copyfile, source, target)
            item["file"] = {"url": f"files/{file_id}/{name}", "name": name, "size": target.stat().st_size,
                            "mime": reply.file.mime or mimetypes.guess_type(name)[0] or ""}
        task = self.board.task(task_id)
        if task is None:
            return Delivery(True, detail="suppressed")  # a late reply for a task removed from the page
        reporting = task["data"].get("reporting") or {}
        if reporting and reply.in_reply_to == reporting.get("request"):  # the report's own result
            try:
                sent = "message_id" in json.loads(reply.data.get("result") or "{}")
            except ValueError:
                sent = False
            self.board.update_task(task_id, data={"reporting": None, **({"reported": {
                key: reporting[key] for key in ("at", "to", "status")}} if sent else {})})
        stored = self.board.add_message(task_id, item)
        return Delivery(True, message_id=str(stored["id"]))

    # ---- live updates -----------------------------------------------------------------------------
    def _on_board(self, event: str, payload: dict) -> None:
        if self._loop is None:
            return
        if event in ("task", "step") and self._publishing is None:  # who is where may have changed (once a moment)
            self._publishing = self._loop.call_later(0.5, self._publish_now)
        if event == "task":
            message = {"type": "task", "task": self._task_view(payload)}
        elif event == "step":
            message = {"type": "step", "step": payload}
        elif event in ("message", "message_update"):
            message = {"type": event, "item": payload}
        elif event == "deleted":
            message = {"type": "deleted", "id": payload["id"]}
        elif event == "projects":
            message = {"type": "projects", "items": self._projects()}
        elif event == "kv" and payload.get("key") == "ready":
            message = {"type": "team", "team": self._team()}
        else:
            return
        self._loop.call_soon(self._broadcast, message)  # (in the order the board changed)

    def _publish_now(self) -> None:
        self._publishing = None
        try:
            self._publish()
        except Exception as error:  # the map is a nicety: it never stops the lab
            self.port.log("places_failed", error=type(error).__name__)

    def _broadcast(self, message: dict) -> None:
        for page in list(self.sockets):
            self._tell(page, message)

    # ---- views --------------------------------------------------------------------------------------
    def _task_view(self, task: dict) -> dict:
        return views.task_view(self.board, task, self.name + ":", self._approval_state)

    def _approval_state(self, approval_id: str) -> str | None:
        """What the core says of an approval (contracts 1.7); a card decided or expired elsewhere is closed here too,
        so every open page stops offering its buttons."""
        asking = getattr(self.port, "approval_status", None)
        return asking(approval_id) if callable(asking) and approval_id else None

    def _projects(self) -> list[dict]:
        return [project.to_dict() for project in self.registry.all().values()]

    def _team(self) -> list[dict]:
        return [{"persona": seat.persona, "name": seat.name, "family": seat.family, "voice": seat.voice,
                 "avatar": seat.avatar or seat.persona, "cheap": seat.label("cheap"), "strong": seat.label("strong"),
                 "ready": self._ready(seat)} for seat in self.team.seats.values()]

    def _ready(self, seat) -> bool | None:
        """Whether she has a key: the core can tell (contracts 1.7), else what the latest run saw."""
        asking = getattr(self.port, "models_ready", None)
        if callable(asking):
            try:
                return bool(asking(seat.profile("cheap")))
            except Exception:
                pass
        return self.team.ready.get(seat.persona)

    def _state(self) -> dict:
        team = self._team()
        avatars = sorted({path.stem for directory in self.avatar_dirs for path in directory.iterdir()
                          if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")})
        return {"title": str(self.config.get("title") or "Club Room"), "user": {"name": self.user_name},
                "team": team, "roles": self.team.roles, "avatars": avatars, "projects": self._projects(),
                "tasks": [self._task_view(task) for task in self.board.tasks()],
                "progress": {task: list(runs.values()) for task, runs in self.progress.items()},
                "settings": {"second_approval": bool(self.config.get("second_approval", True)),
                             "commands": str(self.config.get("commands", "allowlist"))},
                "prices": self.prices.status(self.team) if self.prices else None, "workflows": catalog(),
                "places": self.places(), "links": self.links(), "page": self.page}


async def _send(socket_, message: dict) -> bool:
    try:
        await asyncio.wait_for(socket_.send_json(message), timeout=5)
        return True
    except Exception:
        return False


async def _close(socket_) -> None:
    try:
        await asyncio.wait_for(socket_.close(), timeout=5)
    except Exception:
        pass


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip in ipaddress.ip_network("100.64.0.0/10")


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _login_name() -> str:
    import getpass
    try:
        return getpass.getuser().capitalize()
    except Exception:
        return "You"


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


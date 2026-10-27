"""`club`: the lab from a terminal, in the folder you are in, the way codex, claude or grok work.

    club                        talk with the club about this folder (its git repository)
    club "add a --json flag"    the same, starting with that request
    club -p "fix the test"      one go: work until it needs you, print the result, exit (also `club exec ...`)
    club -c                     come back to this folder's open task
    club -C ~/code/app          work in another folder (also -d)
    club --web                  open this folder's task in the lab page
    club status | stop          is the lab running, its projects and tasks; stop the lab club started
    club install                put `club` on your PATH (~/.local/bin)

It is a client of the lab protocol (PROTOCOL.md), like the page, so whatever it does shows there too. A lab that is
already running is used; otherwise one starts in the background (the real core with just the lab), with the keys
`club install` was given. The two approvals stay yours: club asks you for them and never approves on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from .workspace import system

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATA = ROOT / "data" / "workbench"
SETTINGS = DATA / "cli.json"  # what `club install` recorded: the lab's address and how to start it
LAB = "http://127.0.0.1:4157"
REST = {"review", "approved", "answered", "failed", "landed", "discarded", "cancelled"}
CLOSED = {"landed", "discarded"}
WORKFLOWS = ("auto", "single", "cascade", "critique")  # the built-in ones; the lab sends its full list
ICONS = {"read_file": "📄", "list_files": "📁", "search": "🔎", "write_file": "✏️", "edit_file": "✏️",
         "delete_file": "🗑", "run": "▶", "test": "🧪", "diff": "±", "probe_file": "📝", "probe": "⚖"}


# ---- the terminal ---------------------------------------------------------------------------------------------
YES = {"y", "yes", "a", "approve", "ok", "okay", "lgtm", "sure"}
NO = {"n", "no", "deny"}


def read_line(text: str) -> str:
    """One line from the person; the prompt goes to stderr with the notes, so `club -p > out` still shows it."""
    print(text, end="", file=sys.stderr, flush=True)
    return input()


class LabClosed(ConnectionError):
    """The lab's connection is gone (a restart, a crash): reconnect, or leave."""


class Style:
    def __init__(self, stream):
        self.on = stream.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        if self.on and os.name == "nt":  # the Windows console shows colours once asked to (else raw codes)
            os.system("")

    def __call__(self, text: str, *codes: str) -> str:
        names = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35",
                 "cyan": "36"}
        return f"\033[{';'.join(names[code] for code in codes)}m{text}\033[0m" if self.on and codes else text


def ask(question: str, default: bool = False) -> bool:
    """A yes/no question on the terminal (no terminal: the default)."""
    if not sys.stdin.isatty():
        return default
    try:
        answer = read_line(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    except EOFError:
        return default
    return default if not answer else answer in ("y", "yes")


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---- the folder -----------------------------------------------------------------------------------------------
def git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True)


def workplace(path: Path, yes: bool, err) -> Path | None:
    """The repository to work in: the git root of `path`, checked. A plain folder (or one with no commit yet) can
    become a repository with a first commit, if you agree; nothing else is changed."""
    folder = path.expanduser().resolve()
    if not folder.is_dir():
        err(f"{folder} is not a folder.")
        return None
    if shutil.which("git") is None:
        err("git is not installed; the lab works on a git branch of your folder.")
        return None
    top = git(folder, "rev-parse", "--show-toplevel")
    root = Path(top.stdout.strip()) if top.returncode == 0 else folder
    trees = (lab_data() / "worktrees", DATA / "worktrees")  # the lab's copies of tasks, not projects
    if any(place == tree or tree in place.parents for place in (folder, root) for tree in trees):
        err(f"{folder} is one of the lab's task worktrees; run club in your project instead.")
        return None
    fresh = top.returncode != 0 or git(root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0
    if fresh:
        files = [item for item in root.iterdir() if item.name != ".git"]
        what = "not a git repository" if top.returncode != 0 else "a git repository with no commit yet"
        err(f"{root} is {what}. The lab works on a branch of it and lands your approved change as one commit.")
        offer = (f"Make the first commit here{' of its ' + str(len(files)) + ' files' if files else ''}"
                 + (" (git init)" if top.returncode != 0 else "") + "?")
        if not (yes or ask(offer)):
            err("Nothing changed. Run `git init` and commit there, or pass --yes.")
            return None
        if top.returncode != 0 and git(root, "init", "-q", "-b", "main").returncode != 0:
            git(root, "init", "-q")
        git(root, "add", "-A")
        identity = [] if git(root, "config", "user.email").stdout.strip() else \
            ["-c", "user.name=Workbench", "-c", "user.email=workbench@localhost"]  # as the lab's own commits
        made = git(root, *identity, "commit", "-q", "--allow-empty", "-m", f"Start {root.name}")
        if made.returncode != 0:
            err(f"git could not commit: {made.stderr.strip() or made.stdout.strip()}")
            return None
    return root


def describe(root: Path, cwd: Path) -> str:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "?"
    head = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    dirty = [line for line in git(root, "status", "--porcelain").stdout.splitlines() if line.strip()]
    where = f" (you are in {cwd.relative_to(root)})" if cwd != root and root in cwd.parents else ""
    state = "clean" if not dirty else (f"{len(dirty)} uncommitted change{'s' if len(dirty) > 1 else ''}: the club works "
                                       f"from your last commit ({head}) and lands only on a clean checkout")
    return f"{root}{where} · {branch} · {state}"


# ---- the lab --------------------------------------------------------------------------------------------------
def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/presence", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def lab_data(data: str | None = None) -> Path:
    """The lab's data folder: given, as installed, or the default (its log and pid file are kept there)."""
    return Path(data or load_settings().get("data") or DATA).expanduser().resolve()


def start_lab(url: str, extra: list[str], err, data: str | None = None) -> bool:
    """A lab in the background, on this computer, started the way `club install` recorded (its --env/--key)."""
    address = urlparse(url)
    if address.hostname not in ("127.0.0.1", "localhost", "::1"):
        err(f"Can't reach the lab at {url}.")
        return False
    folder = lab_data(data)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "lab.log"
    command = [sys.executable, "-m", "extensions.workbench", *load_settings().get("lab_args", []), *extra,
               *(["--data", str(folder)] if data else []), "--host", "127.0.0.1", "--port", str(address.port or 4157)]
    with log.open("ab") as handle:  # detached: closing this terminal (or Ctrl-C here) leaves the lab running
        process = subprocess.Popen(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, **system.detached_options())
    (folder / "lab.pid").write_text(str(process.pid))
    for _ in range(120):
        if reachable(url):
            err(f"Started the lab in the background at {url} (log: {log}; `club stop` stops it).")
            return True
        if process.poll() is not None:
            break
        time.sleep(0.25)
    tail = log.read_text("utf-8", "replace").splitlines()[-8:]
    err("The lab did not start:\n  " + "\n  ".join(tail))
    return False


class Lab:
    """The lab page's websocket, driven from a thread of its own: events come in on `events`."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.events: queue.Queue = queue.Queue()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.state: dict = self._run(self._connect())

    def _run(self, coroutine, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    async def _connect(self) -> dict:
        import aiohttp
        self.http = aiohttp.ClientSession()
        self.ws = await self.http.ws_connect(f"{self.url}/ws", headers={"Origin": self.url}, heartbeat=30)
        hello = await self.ws.receive_json(timeout=15)
        self.loop.create_task(self._read())
        return hello["state"]

    async def _read(self) -> None:
        try:
            async for message in self.ws:
                if message.type.name == "TEXT":
                    self.events.put(json.loads(message.data))
        finally:
            self.events.put({"type": "closed"})

    def send(self, event: dict) -> None:
        try:
            self._run(self.ws.send_json(event))
        except Exception as error:
            raise LabClosed(str(error) or type(error).__name__) from None

    def get(self, path: str) -> dict:
        async def fetch():
            async with self.http.get(f"{self.url}{path}") as response:
                return await response.json()
        try:
            return self._run(fetch())
        except Exception as error:
            raise LabClosed(str(error) or type(error).__name__) from None

    def close(self) -> None:
        async def shut():
            await self.ws.close()
            await self.http.close()
        try:
            self._run(shut(), 5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if not self.thread.is_alive():
            self.loop.close()


def uid() -> str:
    return f"cli{time.time_ns()}"


# ---- a session in one folder ----------------------------------------------------------------------------------
class Session:
    def __init__(self, lab: Lab, root: Path, arguments, out=sys.stdout, err=sys.stderr):
        self.lab, self.root, self.args = lab, root, arguments
        self.out, self.err = out, err
        self.style = Style(err)
        self.json = bool(getattr(arguments, "json", False))
        self.task: str | None = None
        self.project: dict | None = None
        self.seen: set[int] = set()       # messages shown
        self.acts: dict[str, int] = {}    # step → actions shown
        self.marks: set[str] = set()      # step states shown
        self.asked: set[str] = set()      # approvals answered here
        self.later: set[str] = set()      # approvals left for later (asked again the next time you follow)
        self.expect: float = 0            # a message sent to a task at rest: until it is at work again (or this time)
        self.viewed: str | None = None    # the fingerprint whose diff was shown
        self.crew = self._crew(getattr(arguments, "crew", "") or "")
        self.workflow = getattr(arguments, "workflow", None) or "auto"
        self.mine: set[str] = set()       # texts sent from here (the page echoes them)

    # ---- output -----------------------------------------------------------------------------------------------
    def note(self, text: str, *codes: str) -> None:
        if not self.json:
            print(self.style(text, *codes) if codes else text, file=self.err, flush=True)

    def emit(self, event: dict) -> None:
        if self.json:
            print(json.dumps(event, ensure_ascii=False), file=self.out, flush=True)

    def seat_name(self, persona: str) -> str:
        seat = next((seat for seat in self.lab.state["team"] if seat["persona"] == persona), None)
        return seat["name"] if seat else persona

    def _crew(self, text: str) -> list[str]:
        key = lambda value: re.sub(r"[^a-z0-9]", "", str(value).lower())
        wanted = [key(name) for name in re.split(r"[,\s]+", text) if name.strip()]
        team = self.lab.state["team"]
        return [seat["persona"] for name in wanted for seat in team if name in (key(seat["persona"]), key(seat["name"]))]

    # ---- the project ------------------------------------------------------------------------------------------
    def ensure_project(self) -> bool:
        for project in self.lab.state["projects"]:
            if Path(project["path"]).resolve() == self.root:
                self.project = project
                return True
        taken = {project["name"] for project in self.lab.state["projects"]}
        base = re.sub(r"[^a-z0-9_.-]+", "-", self.root.name.lower()).strip("-._")[:36] or "project"
        name = next(candidate for candidate in (base, *(f"{base}-{n}" for n in range(2, 99))) if candidate not in taken)
        self.lab.send({"type": "add_project", "name": name, "path": str(self.root)})
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                event = self.lab.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event["type"] == "closed":
                raise LabClosed("the lab closed the connection")
            self.absorb(event)
            if event["type"] == "projects":
                found = next((item for item in event["items"] if item["name"] == name), None)
                if found:
                    self.project = found
                    self.note(f"Added this folder to the lab as project “{name}”.", "dim")
                    return True
            elif event["type"] == "error":
                self.note(f"The lab could not add this folder: {event.get('text')}", "red")
                return False
        self.note("The lab did not answer in time.", "red")
        return False

    def tasks(self) -> list[dict]:
        rows = [task for task in self.lab.state["tasks"] if task["project"] == self.project["name"]]
        return sorted(rows, key=lambda task: task["created"], reverse=True)

    def latest_open(self) -> dict | None:
        return next((task for task in self.tasks() if task["status"] not in CLOSED and task.get("own", True)), None)

    def link(self) -> str:
        return f"{self.lab.url}/#{self.task}/changes" if self.task else f"{self.lab.url}/"

    # ---- talking ----------------------------------------------------------------------------------------------
    def say(self, text: str) -> bool:
        """Start a task with `text`, or add it to the open one."""
        task = next((item for item in self.lab.state["tasks"] if item["id"] == self.task), None)
        self.mine.add(text.strip())
        if task and task["status"] not in CLOSED:
            self.lab.send({"type": "send", "task": self.task, "id": uid(), "text": text})
            if task["status"] in REST:  # its old outcome is not the answer to this: wait for the club to pick it up
                self.expect = time.time() + 15
            return True
        client = uid()
        self.lab.send({"type": "start", "id": client, "project": self.project["name"], "workflow": self.workflow,
                       "text": text, "crew": self.crew})
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                event = self.lab.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if event["type"] == "closed":
                raise LabClosed("the lab closed the connection")
            self.absorb(event)
            if event["type"] == "ack" and event.get("id") == client:
                if not event.get("ok"):
                    self.note(f"Not started: {event.get('reason')}", "red")
                    return False
                self.task = event["task"]
                self.catch_up()
                return True
        self.note("The lab did not answer in time.", "red")
        return False

    def accept(self) -> bool:
        """An invitation from the classroom, taken up here: her brief, who comes along, then start (or not now)."""
        task = self.current() or {}
        invited = (task.get("data") or {}).get("invited") or {}
        crew = list(invited.get("crew") or [])
        self.note(self.style(f"\n🎟️ {invited.get('by') or 'A classmate'} invites you to the lab: {task.get('title')}",
                             "bold", "yellow"))
        if invited.get("notes"):
            self.note(f"   What she knows about how you like it: {invited['notes']}", "dim")
        self.note(f"   The brief:\n   {task['data'].get('request', '').strip()}")
        away = [persona for persona in crew if next((seat for seat in self.lab.state["team"]
                                                     if seat["persona"] == persona), {}).get("ready") is False]
        crew = [persona for persona in crew if persona not in away]
        if crew or away:
            self.note(f"   Coming along: {', '.join(map(self.seat_name, crew)) or 'nobody'}"
                      + (f" ({', '.join(map(self.seat_name, away))} can't come: no key)" if away else ""))
        answer = self.prompt("   [Enter] start · [e] edit the brief · [c] change who comes · [n] not now › ")
        if answer is None or answer.lower() in ("n", "no"):
            return False
        text = task["data"].get("request", "")
        if answer.lower() == "e":
            text = self.prompt("   The brief (one line): ") or text
        if answer.lower() == "c":
            crew = self._crew(self.prompt("   Who comes (names, e.g. pro, gemini): ") or "")
        client = uid()
        self.lab.send({"type": "accept", "task": self.task, "id": client, "text": text, "project": task["project"],
                       "workflow": self.workflow, "crew": crew})
        self.mine.add(text.strip())
        return True

    def catch_up(self) -> None:
        """What happened in the task so far (on start, and when coming back to it)."""
        view = self.lab.get(f"/api/tasks/{self.task}")
        self.absorb({"type": "task", "task": view["task"]})
        story = [(step["started"], 0, "step", step) for step in view["steps"]]
        story += [(item.get("at", 0), 1, "message", item) for item in view["messages"]]
        for _, _, kind, entry in sorted(story, key=lambda row: row[:2]):  # in the order it happened
            self.show_step(entry) if kind == "step" else self.show_message(entry, earlier=True)

    def absorb(self, event: dict) -> None:
        """Keep the lab's state current (tasks, projects, team) from its events."""
        kind = event.get("type")
        if kind == "task":
            tasks = [item for item in self.lab.state["tasks"] if item["id"] != event["task"]["id"]]
            self.lab.state["tasks"] = [event["task"], *tasks]
        elif kind == "projects":
            self.lab.state["projects"] = event["items"]
        elif kind == "team":
            self.lab.state["team"] = event["team"]
        elif kind == "deleted":
            self.lab.state["tasks"] = [item for item in self.lab.state["tasks"] if item["id"] != event["id"]]

    def current(self) -> dict | None:
        return next((item for item in self.lab.state["tasks"] if item["id"] == self.task), None)

    def follow(self, interactive: bool) -> str:
        """Show the work as it happens until the club needs you or is done. Returns the task's status. An approval
        left for later is asked again the next time (not in this one)."""
        quiet_since = None
        self.later.clear()
        while True:
            try:
                event = self.lab.events.get(timeout=0.3)
            except queue.Empty:
                event = None
            if event:
                if event["type"] == "closed":
                    raise LabClosed("the lab closed the connection")
                self.absorb(event)
                about = (event.get("task") or {}).get("id") if event["type"] == "task" else \
                    (event.get("step") or event.get("item") or {}).get("task") or event.get("task")
                if about == self.task:  # (events of other tasks only keep the state current)
                    self.emit(event)
                    if event["type"] == "step":
                        self.show_step(event["step"])
                    elif event["type"] in ("message", "message_update"):
                        self.show_message(event["item"])
                    quiet_since = None
            task = self.current() or {}
            if self.expect and (task.get("status") not in REST or time.time() > self.expect):
                self.expect = 0  # the club has it (or never took it: then what the task says now is the answer)
            if self.expect:
                continue
            pending = self.pending()
            if pending and interactive and pending.get("id") not in self.later:
                if not self.decide(pending):
                    return (self.current() or {}).get("status", "")
                continue
            if pending or task.get("status") in REST:
                quiet_since = quiet_since or time.time()
                # at review its approval card follows the status; give it a moment to arrive
                wait = 0.6 if pending else 6 if task.get("status") in ("review", "approved") else 1.5
                if time.time() - quiet_since > wait:
                    return task.get("status", "")

    # ---- showing ----------------------------------------------------------------------------------------------
    def show_step(self, step: dict) -> None:
        data, key = step.get("data") or {}, step["id"]
        who = self.seat_name(step.get("seat") or "") if step.get("seat") else ""
        actions = data.get("actions") or []
        for action in actions[self.acts.get(key, 0):]:
            if action.get("status") == "waiting":
                break  # shown with its approval
            arguments = action.get("arguments") or {}
            target = arguments.get("path") or arguments.get("command") or arguments.get("pattern") or ""
            extra = action.get("data") or {}
            mark = "✗" if action.get("status") == "failed" else ""
            if action.get("tool") == "run" and extra.get("exit_code") is not None:
                mark = "✓" if extra["exit_code"] == 0 else f"✗ {extra['exit_code']}"
            if extra.get("added") is not None or extra.get("removed") is not None:
                mark = f"+{extra.get('added', 0)} −{extra.get('removed', 0)}"
            if action.get("tool") == "test" and extra.get("passed") is not None:
                mark = "✓" if extra["passed"] else "✗"
            if action.get("tool") == "probe" and extra.get("same") is not None:
                mark = "old = new" if extra["same"] else "old ≠ new"
            line = f"   {ICONS.get(action.get('tool'), '•')} {who + ' ' if who else ''}{action.get('tool', '').replace('_', ' ')}"
            self.note(f"{line} {target}".rstrip() + (f"  {mark}" if mark else ""), "dim")
            self.acts[key] = self.acts.get(key, 0) + 1
        if step.get("kind") == "route" and step["status"] == "done" and f"{key}:end" not in self.marks:
            self.marks.add(f"{key}:end")
            self.note(f"   ↳ {step.get('title')}", "dim")
        if step.get("kind") == "requirements" and step["status"] == "done" and f"{key}:end" not in self.marks:
            self.marks.add(f"{key}:end")  # what "done" means, as the lab wrote it down
            self.note(f"   📋 {step.get('title')}", "dim")
            for item in data.get("requirements") or ():
                self.note(f"      {item['id']} ({item['kind']}) {item['text']}", "dim")
        if data.get("mode") == "probe" and step["status"] == "done" and data.get("status") in ("done", "blocked") \
                and isinstance(data.get("findings"), list) and f"{key}:found" not in self.marks:  # her last turn
            self.marks.add(f"{key}:found")  # the tester's verdict
            findings = data["findings"]
            self.note(f"   🧪 {who}: " + (f"found {len(findings)} problem(s)" if findings else "nothing broke"),
                      "yellow" if findings else "green")
            for item in findings:
                self.note(f"      ✗ {item.get('criterion')}: {item.get('problem')}", "yellow")
        if step["status"] != "running" and step.get("kind") in ("review", "validate", "route"):
            self.marks.add(f"{key}:seen")  # finished: a replay of it is not news
        if step.get("kind") in ("review", "validate") and step["status"] == "running" and not \
                self.marks & {f"{key}:run", f"{key}:seen"}:
            self.marks.add(f"{key}:run")
            self.note(f"   ⧗ {step.get('title') or step['kind']}…", "dim")
        if step.get("kind") == "validate" and step["status"] in ("done", "failed") and f"{key}:end" not in self.marks:
            self.marks.add(f"{key}:end")
            summary = data.get("summary") or step["status"]
            self.note(f"   {'✓' if data.get('passed') else '✗' if data.get('passed') is False else '·'} tests: {summary}",
                      "green" if data.get("passed") else "red" if data.get("passed") is False else "dim")

    def show_message(self, item: dict, earlier: bool = False) -> None:
        if item.get("id") in self.seen:
            return
        self.seen.add(item.get("id"))
        kind = item.get("kind")
        if kind == "text" and item.get("speaker"):
            name = item["speaker"]
            self.note(f"{self.style(name, 'bold', 'cyan')}  {item['text']}")
        elif kind == "text":
            self.note(item["text"], "dim")
        elif kind == "mine" and earlier:
            self.note(f"{self.style('you', 'bold')}  {item['text']}")
        elif kind == "mine" and item.get("text", "").strip() not in self.mine:
            self.note(f"you (in the page)  {item['text']}", "dim")
        elif kind == "choice" and not earlier:
            self.note(f"   ✓ {item.get('text')}", "dim")
        elif kind == "file" and item.get("file"):
            self.note(f"   📎 {item['file'].get('name')}", "dim")

    # ---- approvals --------------------------------------------------------------------------------------------
    def pending(self) -> dict | None:
        """The approval this task waits for now, as the lab says (its views decide; club only asks you)."""
        approval = (self.current() or {}).get("approval")
        return approval if approval and approval.get("id") not in self.asked else None

    def decide(self, card: dict) -> bool:
        """Ask about one approval. Returns False when you leave it for later."""
        capability, approval = card.get("capability", ""), card.get("id", "")
        task = self.current() or {}
        change = (task.get("data") or {}).get("change") or {}
        validation = (task.get("data") or {}).get("validation") or {}
        style = self.style
        print(file=self.err)
        if capability == "workbench.review":
            review = (task.get("data") or {}).get("review") or {}
            facts = [change.get("stat", ""), f"tests: {validation.get('summary')}" if validation.get("ran") else "no tests ran"]
            if review.get("seat"):
                facts.append(f"checked by {self.seat_name(review['seat'])}")
            self.note(style("🔍 Second check: look at everything", "bold", "yellow") + "  " + " · ".join(filter(None, facts)))
            items = card.get("checklist") or []
            for number, entry in enumerate(items, 1):
                self.note(f"   {'☑' if entry.get('ticked') else '☐'} {number}. {entry.get('label')}")
            while True:
                choice = self.prompt("   [d] diff · [y] approve after looking at all of it · [n] no · or type what "
                                     "to change · Enter: later\n   › ")
                if choice is None or choice == "":
                    self.note("   Left for later: /resume here, `club -c`, or the page.", "dim")
                    self.later.add(approval)
                    return False
                if choice.lower() in ("d", "diff"):
                    self.show_diff()
                    continue
                if choice.lower() in YES:
                    if self.viewed != change.get("fingerprint"):
                        self.show_diff()
                    if not ask(f"   You looked at all {len(items)} items and approve them?"):
                        continue
                    for entry in items:  # the same ticks as the page's boxes and `$check`
                        self.lab.send({"type": "tick", "task": self.task, "item": entry.get("id"), "checked": True})
                    self.command(f"$approve {approval}", "Approve")
                    self.asked.add(approval)
                    return True
                if choice.lower() in NO:
                    self.command(f"$deny {approval}", "Not approved")
                    self.asked.add(approval)
                    return True
                if len(choice.split()) < 2 and len(choice) < 12:  # a word that is none of these: ask again
                    self.note("   d, y, n, or what to change in a few words (Enter: later).", "dim")
                    continue
                self.asked.add(approval)
                self.say(choice)  # what to change: the club takes it as feedback
                return True
        if capability == "workbench.land":
            self.note(style("🛬 Final approval", "bold", "yellow") + f"  {card.get('summary')}")
        elif capability in ("workbench.run", "workbench.probe"):
            self.note(style("▶ Run a command?", "bold", "yellow") + f"  {card.get('summary') or ''}")
            if card.get("command"):
                self.note(f"   $ {card['command']}", "bold")
        else:
            self.note(style(f"Approval: {capability}", "bold", "yellow") + f"  {card.get('summary')}")
        while True:
            answer = self.prompt("   [y] yes · [n] no · Enter: later › ")
            if answer is None or answer == "":
                self.note("   Left for later: /resume here, `club -c`, or the page.", "dim")
                self.later.add(approval)
                return False
            if answer.lower() in YES | NO:
                break
            self.note("   y or n (Enter: later).", "dim")  # never a denial by a typo
        approve = answer.lower() in YES
        self.command(f"${'approve' if approve else 'deny'} {approval}", "Approve" if approve else "Not approved")
        self.asked.add(approval)
        return True

    def offer_report(self, ask_first: bool = True) -> None:
        """A visit from the classroom that is over: take the result back to the chat it came from."""
        task = self.current() or {}
        data = task.get("data") or {}
        if "report" not in (task.get("actions") or ()):  # the lab says whether there is something to report
            if not ask_first:
                self.note("Nothing to report back: this task did not come from a conversation, or it is reported.", "dim")
            return
        where = str(data["origin_address"]).split(":", 1)[-1]
        if not ask_first or ask(f"Report back to {where} (the chat it came from)?", True):
            self.lab.send({"type": "report", "task": self.task})
            self.note(f"   Reported back to {where}.", "dim")

    def command(self, text: str, action: str) -> None:
        self.lab.send({"type": "send", "task": self.task, "id": uid(), "text": text, "action": action})

    def prompt(self, text: str) -> str | None:
        try:
            return read_line(text).strip()
        except EOFError:
            return None

    def reconnect(self) -> bool:
        """The lab went away (restarted, crashed): connect again for up to half a minute, and catch up."""
        self.note("\n   The lab closed the connection; reconnecting…", "yellow")
        url = self.lab.url
        self.lab.close()
        for _ in range(60):
            if reachable(url):
                try:
                    self.lab = Lab(url)
                except Exception:
                    pass
                else:
                    self.note("   Back.", "dim")
                    if self.task:
                        self.catch_up()
                    return True
            time.sleep(0.5)
        self.note(f"   The lab is not back at {url}; `club` starts it again next time.", "red")
        return False

    def show_diff(self) -> None:
        change = self.lab.get(f"/api/tasks/{self.task}/diff")
        patch = str(change.get("patch") or "")
        self.viewed = change.get("fingerprint") or ((self.current() or {}).get("data") or {}).get("change", {}).get("fingerprint")
        if not patch.strip():
            self.note("   (no changes)", "dim")
            return
        lines = []
        for line in patch.splitlines():
            color = ("bold",) if line.startswith(("diff --git", "+++", "---")) else ("green",) if line.startswith("+") \
                else ("red",) if line.startswith("-") else ("cyan",) if line.startswith("@@") else ()
            lines.append(self.style(line, *color))
        text = "\n".join(lines)
        height = shutil.get_terminal_size((100, 40)).lines
        pager = os.environ.get("PAGER") or ("less -R" if shutil.which("less") else "")
        if self.err.isatty() and pager and len(lines) > height - 4:
            subprocess.run(pager, shell=True, input=text, text=True)
        else:
            print(text, file=self.err)


# ---- commands ---------------------------------------------------------------------------------------------------
HELP = """Commands:
  /diff              the change set so far
  /status            this folder, the task and the lab
  /tasks             this project's recent tasks
  /resume [id]       go back to a task (the latest open one without an id)
  /new               the next message starts a new task
  /discard           throw the current task's change set away
  /report            take a finished visit's result back to the chat it came from
  /crew [names]      who comes along on new tasks (none: the usual roles)
  /workflow [name]   auto, single, cascade or critique for new tasks
  /web               open this task in the lab page
  /exit              leave (the club keeps working; `club -c` comes back)
Anything else goes to the club: a new task, or more for the current one."""


def interactive(session: Session, first: str | None) -> int:
    try:
        import readline  # noqa: F401  (line editing and history for input())
    except ImportError:
        pass
    style = session.style
    team = [seat["name"] for seat in session.lab.state["team"] if seat.get("ready") is not False]
    session.note(style("🧪 club", "bold") + style(" · the lab, from here", "dim"))
    session.note(f"   📁 {describe(session.root, Path.cwd().resolve())}")
    tests = session.project.get("test_line") or session.project.get("test")
    session.note(f"   🧪 tests: {tests + (' (guessed)' if session.project.get('detected') else '') if tests else 'none found'}"
                 f" · 👥 {', '.join(team) or 'nobody has a key yet'} · {session.lab.url}")
    session.note(style("   Describe a change or ask about the code. /help for commands, Ctrl+D to leave.", "dim"))
    def back() -> None:  # the open task, as it stands, then what it waits for
        if session.task and (session.current() or {}).get("status") == "invited" and not session.accept():
            session.task = None
        if session.task:
            session.note(style(f"\nBack to “{(session.current() or {}).get('title', session.task)}”", "bold"))
            session.catch_up()
            session.follow(True)
            session.offer_report()

    pending, work = first, back
    while True:
        prompting = False
        try:
            if work is not None:
                work, todo = None, work
                todo()
            if pending is None:
                prompting = True
                pending = read_line(style("\n› ", "bold"))
                prompting = False
            text, pending = pending.strip(), None
            if not text:
                continue
            if text.startswith("/"):
                if slash(session, text) == "exit":
                    return 0
                continue
            if session.say(text):
                session.follow(True)
                session.offer_report()
        except EOFError:
            session.note("\nBye. The club keeps any open task; `club -c` comes back to it.", "dim")
            return 0
        except KeyboardInterrupt:
            if prompting:
                session.note("\nBye.", "dim")
                return 0
            session.note("\n   Stepped away; the club keeps working. Type to add something, /resume to watch again.",
                         "dim")
            pending = None
        except LabClosed:
            if not session.reconnect():
                return 1
            pending = None


def slash(session: Session, text: str) -> str:
    name, _, rest = text[1:].partition(" ")
    rest = rest.strip()
    if name in ("exit", "quit", "q"):
        return "exit"
    if name == "help":
        session.note(HELP)
    elif name == "diff":
        session.show_diff() if session.task else session.note("No task yet.", "dim")
    elif name == "status":
        task = session.current()
        session.note(f"📁 {describe(session.root, Path.cwd().resolve())}")
        session.note(f"🧪 project {session.project['name']} · lab {session.lab.url} · workflow {session.workflow}"
                     f" · crew {', '.join(map(session.seat_name, session.crew)) or 'the usual roles'}")
        if task:
            session.note(f"📌 {task['title']} · {task['status']}{' · ' + task['phase'] if task.get('phase') else ''}"
                         f" · {session.link()}")
    elif name == "tasks":
        for task in session.tasks()[:12]:
            mark = "→" if task["id"] == session.task else " "
            session.note(f" {mark} {task['id']}  {task['status']:<10} {task['title']}")
    elif name in ("resume", "open"):
        task = next((item for item in session.tasks() if item["id"] == rest), None) if rest else session.latest_open()
        if task is None:
            session.note("No such task here." if rest else "No open task here.", "dim")
        else:
            session.task = task["id"]
            session.note(f"Back to “{task['title']}” ({task['status']})", "bold")
            session.catch_up()
            session.follow(True)
            session.offer_report()
    elif name == "new":
        session.task = None
        session.note("The next message starts a new task.", "dim")
    elif name == "discard":
        if session.task and ask(f"Throw away “{(session.current() or {}).get('title')}” and its change set?"):
            session.command(f"$discard {session.task}", "Discard")
            session.follow(False)
    elif name == "crew":
        session.crew = session._crew(rest)
        session.note(f"Crew for new tasks: {', '.join(map(session.seat_name, session.crew)) or 'the usual roles'}", "dim")
    elif name == "workflow":
        known = [item["name"] for item in session.lab.state.get("workflows") or ()] or list(WORKFLOWS)
        if rest in known:
            session.workflow = rest
        session.note(f"Workflow for new tasks: {session.workflow} (one of {', '.join(known)})", "dim")
    elif name == "report":
        session.offer_report(ask_first=False) if session.task else session.note("No task yet.", "dim")
    elif name == "web":
        session.note(session.link())
        webbrowser.open(session.link())
    else:
        session.note(f"Unknown command /{name}. /help lists them.", "dim")
    return ""


def once(session: Session, text: str) -> int:
    """`club -p`: one go, printed for scripts; it stops where your approval is needed (never giving it)."""
    try:
        if not session.say(text):
            return 1
        status = session.follow(False)
    except KeyboardInterrupt:
        session.note("\nStepped away; the club keeps working.", "dim")
        return 130
    except LabClosed as error:
        session.note(f"The lab went away ({error}); the club keeps any task it had: `club -c` comes back to it.", "red")
        return 1
    task = session.current() or {}
    data = task.get("data") or {}
    result = {"type": "result", "task": session.task, "status": status, "title": task.get("title"),
              "summary": data.get("summary") or data.get("answer") or data.get("failure") or "",
              "change": (data.get("change") or {}).get("stat"), "tests": (data.get("validation") or {}).get("summary"),
              "link": session.link()}
    if task.get("approval") and status not in ("review", "approved"):  # mid-work: a command waits for you
        result["summary"] = f"Waiting for your approval: {task['approval'].get('summary')}"
        result["approval"] = task["approval"]
    if session.json:
        session.emit(result)
    else:
        print(result["summary"] or status, file=session.out)
        if status in ("review", "approved") or task.get("approval"):
            session.note(f"\nWaiting for you: run `club -c` in {session.root}, or open {session.link()}", "yellow")
    return 0 if status in ("review", "approved", "answered", "landed") or task.get("approval") else 1


# ---- install, status, stop --------------------------------------------------------------------------------------
def install(arguments) -> int:
    folder = Path(arguments.bin).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    launcher = folder / arguments.name
    launcher.write_text(f"""#!{sys.executable}
# {arguments.name}: the club's lab from any folder (written by `python -m extensions.workbench install`).
import sys
sys.path.insert(0, {str(ROOT)!r})
from extensions.workbench.cli import main
sys.exit(main())
""", encoding="utf-8")
    launcher.chmod(0o755)
    if os.name == "nt":  # Windows runs .cmd files, not shebang scripts
        launcher.with_suffix(".cmd").write_text(f'@"{sys.executable}" "{launcher}" %*\r\n', encoding="utf-8")
    lab_args = []
    for path in arguments.env:
        lab_args += ["--env", str(Path(path).expanduser().resolve())]
    for name in arguments.key:
        lab_args += ["--key", name]
    if arguments.data:
        lab_args += ["--data", str(Path(arguments.data).expanduser().resolve())]
    if arguments.demo:
        lab_args.append("--demo")
    DATA.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps({"lab": arguments.lab or LAB, "lab_args": lab_args, "data": str(
        Path(arguments.data).expanduser().resolve()) if arguments.data else ""}, indent=2), encoding="utf-8")
    print(f"Installed {launcher}.")
    if str(folder.resolve()) not in [str(Path(item).expanduser().resolve()) for item in os.environ.get("PATH", "").split(os.pathsep) if item]:
        print(f"{folder} is not on your PATH yet; add this to ~/.bashrc (or ~/.zshrc):\n  export PATH=\"{folder}:$PATH\"")
    print(f"Then, in any project folder: {arguments.name}")
    return 0


def status(url: str) -> int:
    if not reachable(url):
        print(f"No lab at {url}. `club` starts one when you use it.")
        return 1
    with urllib.request.urlopen(f"{url}/api/presence", timeout=3) as response:
        presence = json.loads(response.read())
    lab = Lab(url)
    try:
        print(f"Lab at {url} · {presence.get('working', 0)} at work · {presence.get('ready', 0)} for you to check")
        for project in lab.state["projects"]:
            tasks = [task for task in lab.state["tasks"] if task["project"] == project["name"]
                     and task["status"] not in CLOSED]
            print(f"  {project['name']:<18} {project['path']}" + (f" · {len(tasks)} open" if tasks else ""))
    finally:
        lab.close()
    return 0


def stop(data: str | None = None) -> int:
    pid_file = lab_data(data) / "lab.pid"
    try:
        pid = int(pid_file.read_text())
    except (OSError, ValueError):
        pid = 0
    folder = str(lab_data(data))
    # Started as `python -m extensions.workbench`, it hands over to `python -m core --config <its data>/core/core.yml`.
    ours = lambda: system.process_running(pid, "extensions.workbench") or system.process_running(pid, folder)
    if not pid or not ours():
        print("No lab started by club is running.")
        return 1
    system.stop_process(pid)
    for _ in range(120):  # the core finishes its runs' current steps first (its supervisor gives it 20 s)
        if not system.process_running(pid):
            break
        time.sleep(0.25)
    else:
        print(f"The lab (pid {pid}) is still stopping; `club stop` again, or wait.")
        return 1
    pid_file.unlink(missing_ok=True)
    print("Stopped the lab; its runs and approvals carry over to the next start.")
    return 0


# ---- main -------------------------------------------------------------------------------------------------------
def parser() -> argparse.ArgumentParser:
    commands = argparse.ArgumentParser(prog="club", description=__doc__.split("\n\n")[0],
                                       formatter_class=argparse.RawDescriptionHelpFormatter,
                                       epilog=__doc__.split("\n\n", 1)[1])
    commands.add_argument("prompt", nargs="*", help="what to work on (or exec/status/stop/install)")
    commands.add_argument("-p", "--print", dest="once", action="store_true",
                          help="one go: work until the club needs you, print the result, exit")
    commands.add_argument("-c", "--continue", dest="resume", action="store_true", help="come back to this folder's open task")
    commands.add_argument("-C", "-d", "--cd", dest="folder", help="work in this folder instead of the current one")
    commands.add_argument("--web", action="store_true", help="open this folder's task in the lab page")
    commands.add_argument("--json", action="store_true", help="with -p: every event as a JSON line, then the result")
    commands.add_argument("--workflow", help="how to work: auto (default), single, cascade, critique, or one "
                                               "added under workflows/")
    commands.add_argument("--crew", default="", help="who comes along, e.g. flash,pro (default: the usual roles)")
    commands.add_argument("--lab", help=f"the lab's address (default {LAB}, or CLUB_LAB)")
    commands.add_argument("-y", "--yes", action="store_true", help="make the folder a git repository without asking")
    commands.add_argument("--env", action="append", default=[], help="install / starting a lab: a .env file with keys")
    commands.add_argument("--key", action="append", default=[], metavar="NAME", help="install / starting a lab: only this key")
    commands.add_argument("--bin", default="~/.local/bin", help="install: where to put the command")
    commands.add_argument("--name", default="club", help="install: the command's name")
    commands.add_argument("--data", help="install / starting a lab: the lab's data folder")
    commands.add_argument("--demo", action="store_true", help="install / starting a lab: the scripted club, no keys")
    return commands


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    words = list(arguments.prompt)
    verb = words[0] if words and words[0] in ("exec", "status", "stop", "install") else ""
    if verb:
        words = words[1:]
    url = (arguments.lab or os.environ.get("CLUB_LAB") or load_settings().get("lab") or LAB).rstrip("/")
    if verb == "install":
        return install(arguments)
    if verb == "status":
        return status(url)
    if verb == "stop":
        return stop(arguments.data)
    err = lambda text: print(text, file=sys.stderr)
    root = workplace(Path(arguments.folder or os.getcwd()), arguments.yes, err)
    if root is None:
        return 2
    extra = [*sum((["--env", str(Path(item).expanduser().resolve())] for item in arguments.env), []),
             *sum((["--key", name] for name in arguments.key), []), *(["--demo"] if arguments.demo else [])]
    if not reachable(url) and not start_lab(url, extra, err, arguments.data):
        return 1
    lab = Lab(url)
    try:
        session = Session(lab, root, arguments)
        if not session.ensure_project():
            return 1
        if arguments.resume:
            task = session.latest_open()
            if task is None:
                err("No open task in this folder; starting fresh.")
            else:
                session.task = task["id"]
        text = " ".join(words).strip()
        if not text and not sys.stdin.isatty() and (arguments.once or verb == "exec"):
            text = sys.stdin.read().strip()  # `echo "..." | club -p`
        if arguments.web:
            session.task = session.task or (session.latest_open() or {}).get("id")
            print(session.link())
            webbrowser.open(session.link())
            return 0
        if arguments.once or verb == "exec":
            if not text:
                err("Nothing to do: give the request, e.g. club -p \"fix the failing test\".")
                return 2
            return once(session, text)
        return interactive(session, text or None)
    finally:
        lab.close()


if __name__ == "__main__":
    sys.exit(main())

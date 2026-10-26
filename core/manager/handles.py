"""Handles the Manager gives engines and capabilities. Every call goes back through the Manager."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import datetime as dt
import json
import math
import time

from contracts import (Activity, Address, Delivery, Display, Event, FileRef, HistoryItem, HistorySummary, ModelError,
                       ModelRequest, ModelResult, Persona, Request, Sender, ToolResult)
from core.manager.diary import DiaryHandle
from core.serialize import request_from_dict
from core.storage.tank import RunRecord

if TYPE_CHECKING:
    from core.manager.manager import Manager


class ModelsHandle:
    def __init__(self, manager: "Manager", run_id: str, default_profile: str):
        self._manager, self._run_id, self._default_profile = manager, run_id, default_profile

    async def generate(self, request: ModelRequest) -> ModelResult:
        return await self._manager.generate(self._run_id, request, request.profile or self._default_profile)

    def profiles(self) -> Sequence[str]:
        return self._manager.providers.profiles()

    def ready(self, profile: str | None = None) -> bool:
        return self._manager.providers.profile_ready(profile or self._default_profile)


class ToolsHandle:
    def __init__(self, manager: "Manager", run_id: str):
        self._manager, self._run_id = manager, run_id

    def list(self) -> list[dict[str, Any]]:
        return self._manager.tool_schemas(self._run_id)

    async def call(self, name: str, arguments: Mapping[str, Any], call_id: str | None = None) -> ToolResult:
        return await self._manager.call_tool(self._run_id, name, dict(arguments or {}), call_id)


SUMMARY_CHARS = 8000


class HistoryHandle:
    def __init__(self, manager: "Manager", address: Address):
        self._manager, self._address = manager, address

    def recent(self, limit: int = 30) -> list[HistoryItem]:
        return self._manager.tank.recent_messages(str(self._address), limit)

    def search(self, text: str, limit: int = 20) -> list[HistoryItem]:
        text = str(text or "").strip()
        return self._manager.tank.search_messages(str(self._address), text, limit) if text else []

    def summary(self) -> HistorySummary | None:
        value = self._manager.tank.store(SUMMARIES).get(str(self._address))
        return HistorySummary(value["text"], float(value["upto"]), float(value["at"])) if value else None

    def summarize(self, text: str, upto: float) -> None:
        if not isinstance(text, str):
            raise ValueError("A summary is text")
        text = text.strip()
        if not text or len(text) > SUMMARY_CHARS:
            raise ValueError(f"A summary is 1 to {SUMMARY_CHARS} characters")
        try:
            upto = float(upto)
        except (TypeError, ValueError):
            raise ValueError("upto is the time of the newest message the summary covers") from None
        if not math.isfinite(upto):
            raise ValueError("upto is the time of the newest message the summary covers")
        # Scrubbing can lengthen it ("[secret NAME]" for a short value): what is stored stays within the limit.
        self._manager.tank.store(SUMMARIES).set(str(self._address), {
            "text": self._manager.secrets.scrub(text)[:SUMMARY_CHARS], "upto": upto, "at": time.time()})


SUMMARIES = "core:summaries"  # conversation address → its rolling summary (History.summarize)


class ReadingHistory:
    """A capability's view of its conversation's history (contracts 1.6): it reads; only engines write the summary."""

    def __init__(self, history: HistoryHandle):
        self._history = history

    def recent(self, limit: int = 30) -> list[HistoryItem]:
        return self._history.recent(limit)

    def search(self, text: str, limit: int = 20) -> list[HistoryItem]:
        return self._history.search(text, limit)

    def summary(self) -> HistorySummary | None:
        return self._history.summary()

    def summarize(self, text: str, upto: float) -> None:
        raise ValueError("A capability reads the conversation's history; only engines write its summary")


def local_time(at: float) -> str:
    """A stored time for people, `YYYY-MM-DD HH:MM` in local time; one an interface recorded wrongly (in
    milliseconds, say) is shown as unknown instead of failing whatever shows it."""
    try:
        return dt.datetime.fromtimestamp(at).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError, TypeError):
        return "unknown time"


def module_store(manager: "Manager", module: str, address: Address, sender: Sender, personas: Sequence[str], *,
                 conversation: bool, persona: str | None, person: bool):
    """A module's store, scoped by any combination of conversation, persona and person (contracts 1.5). The
    module's name comes first, so no module can reach another's; with no scope it is the module's store
    everywhere (the same as ToolContext.storage)."""
    parts: dict[str, str] = {}
    if conversation:
        parts["conversation"] = str(address)
    if persona is not None:
        if persona not in personas:
            raise ValueError(f"No persona {persona!r} in this run")
        parts["persona"] = persona
    if person:
        if not sender.id:
            raise ValueError("This run has no person (a routine without a sender)")
        parts["person"] = person_id(address, sender)
    # The parts as JSON, so no conversation, persona or sender ID can spell another scope's name.
    scope = "extension:" + module + (" " + json.dumps(parts, ensure_ascii=False, sort_keys=True) if parts else "")
    return manager.tank.store(scope)


def person_id(address: Address, sender: Sender) -> str:
    """A person across conversations: sender IDs are only unique within one interface, so it leads."""
    return json.dumps([address.interface, sender.id], ensure_ascii=False)


class SessionHandle:
    """Implements contracts.Session for one drive of one run."""

    def __init__(self, manager: "Manager", record: RunRecord, events: list[Event], resumed: bool):
        self._manager = manager
        self.run_id = record.id
        self.address = Address.parse(record.address)
        self.origin = record.origin
        self.trigger: Request = request_from_dict(record.trigger)
        self.display: Display = self.trigger.display
        self.personas: tuple[Persona, ...] = tuple(manager.personas[name] for name in record.personas)
        self.events: tuple[Event, ...] = tuple(events)
        self.state: dict = dict(record.state)
        self.resumed = resumed
        self.options = dict(record.assignment.get("options") or {})
        self.owner = record.origin != "routine" and manager.is_owner(self.trigger)
        self.access = manager.caller_access(record)
        profile = self.personas[0].profile if self.personas else "default"
        self.models = ModelsHandle(manager, record.id, profile)
        self.tools = ToolsHandle(manager, record.id)
        self.history = HistoryHandle(manager, self.address)
        self._module = manager.origin_of("engine:" + record.engine)

    def store(self, *, conversation: bool = False, persona: str | None = None, person: bool = False):
        return module_store(self._manager, self._module, self.address, self.trigger.sender,
                            [item.name for item in self.personas], conversation=conversation, persona=persona,
                            person=person)

    def memory(self, persona: str) -> Any:
        if persona not in {item.name for item in self.personas}:
            raise ValueError("A session can only open memory for its own personas")
        return self._manager.tank.store("persona:" + persona)

    def diary(self, persona: str) -> DiaryHandle:
        if persona not in {item.name for item in self.personas}:
            raise ValueError("A session can only open the diary of its own personas")
        record = self._manager.tank.run(self.run_id)
        subject = self.trigger.sender.id
        return DiaryHandle(self._manager, record, persona, self.trigger.text, subject,
                           chats=self._manager.diary_chats(record.address, self.options, subject))

    def inbox(self) -> list[Request]:
        return self._manager.take_inbox(self.run_id)

    def activity(self) -> list[Activity]:
        return [Activity(**item) for item in self._manager.activity(self.run_id)]

    async def save(self, state: dict) -> None:
        self.state = dict(state)
        self._manager.save_state(self.run_id, self.state)

    async def reply(self, text: str, quote: str | None = None, persona: str | None = None,
                    label: str = "") -> Delivery:
        speaker = ""
        if persona is not None:
            match = next((item for item in self.personas if item.name == persona), None)
            if match is None:
                raise ValueError("A session can only speak as its own personas")
            speaker = match.display_name
        return await self._manager.reply(self.run_id, text, quote, speaker, label, persona=persona)

    async def send_file(self, file: FileRef, caption: str = "") -> Delivery:
        return await self._manager.send_file(self.run_id, file, caption)

    async def progress(self, text: str) -> None:
        await self._manager.progress(self.run_id, text)


class ToolContextHandle:
    """Implements contracts.ToolContext for one capability call."""

    def __init__(self, manager: "Manager", record: RunRecord, extension: str, workspace: Path):
        trigger = request_from_dict(record.trigger)
        self._manager = manager
        self.address: Address = trigger.address
        self.sender: Sender = trigger.sender
        self.origin = record.origin
        self.run_id = record.id
        self.display: Display = trigger.display
        self.is_direct = trigger.is_direct
        files, own = [], []
        for event in manager.tank.events(record.id, ("input", "inbox")):
            request = event.data["request"]
            attached = [FileRef(**item) for item in request.get("files", ())]
            files.extend(attached)
            if str((request.get("sender") or {}).get("id", "")) == trigger.sender.id:
                own.extend(attached)
        self.files: tuple[FileRef, ...] = tuple(files) or tuple(trigger.files)
        self.own_files: tuple[FileRef, ...] = tuple(own) or (tuple(trigger.files) if not files else ())
        self.storage = manager.tank.store("extension:" + extension)
        self._extension, self._personas = extension, list(record.personas)
        profile = (manager.personas[record.personas[0]].profile if record.personas else "default")
        self.models = ModelsHandle(manager, record.id, profile)
        self.workspace = workspace
        self.access = manager.caller_access(record)
        self.history = ReadingHistory(HistoryHandle(manager, self.address))

    async def progress(self, text: str) -> None:
        await self._manager.progress(self.run_id, text)

    def store(self, *, conversation: bool = False, persona: str | None = None, person: bool = False):
        return module_store(self._manager, self._extension, self.address, self.sender, self._personas,
                            conversation=conversation, persona=persona, person=person)

    async def schedule(self, at: float, text: str) -> str:
        return self._manager.gateway.schedule(self.address, at, text, self.sender)

    def scheduled(self) -> list[dict]:
        return self._manager.gateway.scheduled_for(self.address)

    async def unschedule(self, scheduled_id: str) -> bool:
        return self._manager.gateway.cancel_scheduled(self.address, scheduled_id)


def model_error_result(error: ModelError) -> ToolResult:
    return ToolResult.fail(str(error), error.kind)

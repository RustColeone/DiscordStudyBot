"""Handles the Manager gives engines and capabilities. Every call goes back through the Manager."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from contracts import (Activity, Address, Delivery, Display, Event, FileRef, HistoryItem, ModelError, ModelRequest,
                       ModelResult, Persona, Request, Sender, ToolResult)
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


class ToolsHandle:
    def __init__(self, manager: "Manager", run_id: str):
        self._manager, self._run_id = manager, run_id

    def list(self) -> list[dict[str, Any]]:
        return self._manager.tool_schemas(self._run_id)

    async def call(self, name: str, arguments: Mapping[str, Any], call_id: str | None = None) -> ToolResult:
        return await self._manager.call_tool(self._run_id, name, dict(arguments or {}), call_id)


class HistoryHandle:
    def __init__(self, manager: "Manager", address: Address):
        self._manager, self._address = manager, address

    def recent(self, limit: int = 30) -> list[HistoryItem]:
        return self._manager.tank.recent_messages(str(self._address), limit)


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

    def memory(self, persona: str) -> Any:
        if persona not in {item.name for item in self.personas}:
            raise ValueError("A session can only open memory for its own personas")
        return self._manager.tank.store("persona:" + persona)

    def diary(self, persona: str) -> DiaryHandle:
        if persona not in {item.name for item in self.personas}:
            raise ValueError("A session can only open the diary of its own personas")
        record = self._manager.tank.run(self.run_id)
        return DiaryHandle(self._manager, record, persona, self.trigger.text, self.trigger.sender.id)

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
        return await self._manager.reply(self.run_id, text, quote, speaker, label)

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
        files = []
        for event in manager.tank.events(record.id, ("input", "inbox")):
            files.extend(FileRef(**item) for item in event.data["request"].get("files", ()))
        self.files: tuple[FileRef, ...] = tuple(files) or tuple(trigger.files)
        self.storage = manager.tank.store("extension:" + extension)
        profile = (manager.personas[record.personas[0]].profile if record.personas else "default")
        self.models = ModelsHandle(manager, record.id, profile)
        self.workspace = workspace
        self.access = manager.caller_access(record)

    async def progress(self, text: str) -> None:
        await self._manager.progress(self.run_id, text)

    async def schedule(self, at: float, text: str) -> str:
        return self._manager.gateway.schedule(self.address, at, text, self.sender)

    def scheduled(self) -> list[dict]:
        return self._manager.gateway.scheduled_for(self.address)

    async def unschedule(self, scheduled_id: str) -> bool:
        return self._manager.gateway.cancel_scheduled(self.address, scheduled_id)


def model_error_result(error: ModelError) -> ToolResult:
    return ToolResult.fail(str(error), error.kind)

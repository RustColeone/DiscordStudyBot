"""Public contracts between the core and extensions. Extensions import only this package."""

from contracts.capability import TIER_ORDER, Capability, Handler, Risk, ToolContext, ToolResult
from contracts.commands import Command, Option
from contracts.engine import (DIARY_KINDS, Activity, Diary, DiaryNote, Engine, EngineSpec, Event, History,
                              HistoryItem, HistorySummary, Persona, RunYield, Session, Tools)
from contracts.extension import Contributions, Extension, ExtensionContext, Setting
from contracts.interface import Interface, InterfacePort, Operation
from contracts.messages import Action, Address, Delivery, Display, FileRef, Reply, Request, Sender, SubmitResult
from contracts.models import (AdapterFactory, AdapterResult, Image, ModelAdapter, ModelError, ModelRequest,
                              ModelResult, Models, NotConfigured, ToolCall, ToolSpec)
from contracts.stores import Store

# 1.5, plus the tool calls of 1.7 (ToolSpec, ToolCall, ModelRequest.tools), ported ahead from the clone. The
# rest of 1.6 and 1.7 is not here yet, so the minor version stays.
CONTRACTS_VERSION = "1.5"

__all__ = [
    "AdapterFactory", "AdapterResult", "Image", "ModelAdapter", "NotConfigured",
    "Action", "Activity", "Address", "Command", "Option", "Capability", "Contributions", "CONTRACTS_VERSION", "DIARY_KINDS", "Delivery", "Diary",
    "DiaryNote", "Display", "Engine",
    "EngineSpec", "Event", "Extension", "ExtensionContext", "FileRef", "Handler", "History", "HistoryItem",
    "HistorySummary",
    "Interface", "InterfacePort", "ModelError", "ModelRequest", "ModelResult", "Models", "Operation",
    "Persona", "Reply", "Request", "Risk", "TIER_ORDER", "RunYield", "Sender", "Session", "Setting", "Store", "SubmitResult", "ToolContext",
    "ToolCall", "ToolResult", "ToolSpec", "Tools",
]

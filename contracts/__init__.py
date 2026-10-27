"""Public contracts between the core and extensions. Extensions import only this package."""

from contracts.capability import TIER_ORDER, Capability, Handler, Risk, ToolContext, ToolResult
from contracts.commands import Command, Option
from contracts.engine import (DIARY_KINDS, PART_PLACES, Activity, Diary, DiaryNote, Engine, EngineSpec, Event, History,
                              HistoryItem, HistorySummary, PartContext, Persona, PromptPart, RunYield, SHUTDOWN, Session,
                              Tools)
from contracts.extension import Contributions, Extension, ExtensionContext, Setting
from contracts.interface import PLACE_STATUS, Interface, InterfacePort, Operation
from contracts.messages import Action, Address, Delivery, Display, FileRef, Reply, Request, Sender, SubmitResult
from contracts.models import (AdapterFactory, AdapterResult, Image, ModelAdapter, ModelError, ModelRequest,
                              ModelResult, Models, NotConfigured, ToolCall, ToolSpec)
from contracts.stores import Store

# 1.8: 1.7 plus prompt parts (PromptPart, PartContext, Persona.parts, Contributions.prompt_parts, Session.prompt).
CONTRACTS_VERSION = "1.8"

__all__ = [
    "AdapterFactory", "AdapterResult", "Image", "ModelAdapter", "NotConfigured",
    "Action", "Activity", "Address", "Command", "Option", "Capability", "Contributions", "CONTRACTS_VERSION", "DIARY_KINDS", "Delivery", "Diary",
    "DiaryNote", "Display", "Engine",
    "EngineSpec", "Event", "Extension", "ExtensionContext", "FileRef", "Handler", "History", "HistoryItem",
    "HistorySummary",
    "Interface", "InterfacePort", "PLACE_STATUS", "ModelError", "ModelRequest", "ModelResult", "Models", "Operation",
    "PART_PLACES", "PartContext", "Persona", "PromptPart", "Reply", "Request", "Risk", "TIER_ORDER", "RunYield", "SHUTDOWN", "Sender", "Session", "Setting", "Store", "SubmitResult", "ToolContext",
    "ToolCall", "ToolResult", "ToolSpec", "Tools",
]

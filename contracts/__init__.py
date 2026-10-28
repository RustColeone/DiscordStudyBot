"""Public contracts between the core and extensions. Extensions import only this package."""

from contracts.capability import TIER_ORDER, Capability, Handler, Risk, ToolContext, ToolResult
from contracts.commands import Command, Option
from contracts.engine import (DIARY_KINDS, PART_PLACES, Activity, Diary, DiaryNote, Engine, EngineSpec, Event, History,
                              HistoryItem, HistorySummary, PartContext, Persona, PromptPart, RunYield, SHUTDOWN, Session,
                              TIERS, Tools)
from contracts.extension import Contributions, Extension, ExtensionContext, Setting
from contracts.interface import PLACE_STATUS, Interface, InterfacePort, Operation
from contracts.messages import Action, Address, Delivery, Display, FileRef, Reply, Request, Sender, SubmitResult
from contracts.models import (AdapterFactory, AdapterResult, Image, ModelAdapter, ModelError, ModelRequest,
                              ModelResult, Models, NotConfigured, ToolCall, ToolSpec)
from contracts.stores import Facts, Prices, Store
from contracts.watchers import Exchange, WatchContext, Watcher

# 1.8: 1.7 plus prompt parts (PromptPart, PartContext, Persona.parts, Contributions.prompt_parts, Session.prompt).
# 1.9: 1.8 plus watchers (Watcher, Exchange, WatchContext, Contributions.watchers), facts (Facts,
#      ExtensionContext.facts, InterfacePort.facts), InterfacePort.personas, Persona.aliases, .formerly, .pictures,
#      and PartContext.engine.
# 1.10: 1.9 plus declared dependencies (Extension.requires, Extension.uses, ExtensionContext.present).
# 1.11: 1.10 plus former names of engines and modules (EngineSpec.formerly, Extension.formerly), agents described for
#       other engines (Persona.description, Persona.profiles, Persona.tier, TIERS, ExtensionContext.personas).
# 1.12: 1.11 plus model prices (Prices, ModelResult.price and .cost, ExtensionContext.prices, InterfacePort.prices) and
#       stamina (a run that reaches a persona's daily allowance, or its Request.extra `budget_usd`, waits for the
#       owner; InterfacePort.stamina).
CONTRACTS_VERSION = "1.12"

__all__ = [
    "AdapterFactory", "AdapterResult", "Image", "ModelAdapter", "NotConfigured",
    "Action", "Activity", "Address", "Command", "Option", "Capability", "Contributions", "CONTRACTS_VERSION", "DIARY_KINDS", "Delivery", "Diary",
    "DiaryNote", "Display", "Engine",
    "EngineSpec", "Event", "Exchange", "Extension", "ExtensionContext", "Facts", "FileRef", "Handler", "History",
    "HistoryItem",
    "HistorySummary",
    "Interface", "InterfacePort", "PLACE_STATUS", "ModelError", "ModelRequest", "ModelResult", "Models", "Operation",
    "PART_PLACES", "PartContext", "Persona", "Prices", "PromptPart", "Reply", "Request", "Risk", "TIER_ORDER", "TIERS", "RunYield", "SHUTDOWN", "Sender", "Session", "Setting", "Store", "SubmitResult", "ToolContext",
    "ToolCall", "ToolResult", "ToolSpec", "Tools",
    "WatchContext", "Watcher",
]

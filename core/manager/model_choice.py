"""`$model`: which model each persona answers with in one conversation.

A choice (`provider/model`) is tried before the persona's profile, whose candidates stay as the fallback.
Choices are kept per conversation in the tank; the lists of models are asked of the providers live (kept
for an hour). Admins and the creator only; the command is not offered to models.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from contracts import Address, Persona, Risk, ToolResult

if TYPE_CHECKING:
    from core.manager.manager import Manager

USAGE = ("`$model` shows each persona's model here; `$model <provider>` lists that provider's models; "
         "`$model <persona> <provider>/<model>` chooses one here; `$model <persona> reset` goes back to her own.")


def candidate(choice: str) -> dict[str, str]:
    """`provider/model` (the model ID may contain `/` itself, as OpenRouter's do)."""
    provider, _, model = choice.partition("/")
    return {"provider": provider, "model": model}


class ModelChoices:
    def __init__(self, manager: "Manager"):
        self.manager = manager
        self.store = manager.tank.store("core:models")  # address → {persona: "provider/model"}

    def get(self, address: str, persona: str) -> str | None:
        return (self.store.get(address) or {}).get(persona)

    def set(self, address: str, persona: str, choice: str | None) -> None:
        choices = dict(self.store.get(address) or {})
        if choice is None:
            choices.pop(persona, None)
        else:
            choices[persona] = choice
        if choices:
            self.store.set(address, choices)
        else:
            self.store.delete(address)

    def first(self, address: str, persona: str | None) -> dict[str, str] | None:
        """The candidate a model call for this persona here tries first, if one was chosen."""
        choice = self.get(address, persona) if persona else None
        return candidate(choice) if choice else None

    # ---- the command ----------------------------------------------------------------------------------
    def personas_here(self, address: Address) -> list[Persona]:
        assignment = self.manager.assignment_for(address)
        names = assignment.personas if assignment else ()
        return [self.manager.personas[name] for name in names if name in self.manager.personas]

    def current(self, address: str, persona: Persona) -> tuple[str, bool]:
        """(provider/model, chosen): her choice here, else the first candidate of her profile that can answer."""
        choice = self.get(address, persona.name)
        if choice:
            return choice, True
        providers = self.manager.providers
        own = [item for item in providers.candidates(persona.profile) if providers.ready(item["provider"])]
        return (f"{own[0]['provider']}/{own[0]['model']}" if own else ""), False

    async def catalogue(self, names: list[str]) -> tuple[dict[str, list[str] | None], dict[str, str]]:
        """What each provider offers: its list, narrowed by its settings (models_since and so on)."""
        async def one(name: str):
            try:
                return name, await self.manager.providers.list_models(name), ""
            except Exception as error:
                return name, None, f"{type(error).__name__}: {error}"[:200]
        rows = await asyncio.gather(*(one(name) for name in names))
        return {name: models for name, models, _ in rows}, {name: error for name, _, error in rows if error}

    async def command(self, address: Address, text: str) -> ToolResult:
        words = text.split()
        personas = self.personas_here(address)
        available = self.manager.providers.available()
        if len(words) == 1 and words[0].lower() in available:
            return await self._provider_models(words[0].lower())
        if len(words) == 2:
            return await self._choose(address, personas, available, *words)
        if words:
            return ToolResult.fail(USAGE, "invalid_arguments")
        return await self._overview(address, personas, available)

    async def _overview(self, address: Address, personas: list[Persona], available: list[str]) -> ToolResult:
        models, errors = await self.catalogue(available)
        rows = []
        for persona in personas:
            model, chosen = self.current(str(address), persona)
            rows.append({"name": persona.name, "display_name": persona.display_name, "model": model,
                         "chosen": chosen})
        lines = [f"• {row['display_name']}: {row['model'] or 'no model with a key'}"
                 f"{' (chosen here)' if row['chosen'] else ''}" for row in rows] or ["No personas here."]
        counts = ", ".join(f"{name} ({len(models[name])})" if models.get(name) is not None
                           else f"{name} ({'unavailable' if name in errors else 'cannot list'})" for name in available)
        summary = "Models here:\n" + "\n".join(lines) + f"\nProviders: {counts or 'none with a key'}.\n{USAGE}"
        return ToolResult.ok(summary, data={"personas": rows, "models": models, "errors": errors})

    async def _provider_models(self, provider: str) -> ToolResult:
        models, errors = await self.catalogue([provider])
        if provider in errors:
            return ToolResult.fail(f"Could not list {provider}'s models: {errors[provider]}", "unavailable")
        listed = models[provider]
        if listed is None:
            return ToolResult.ok(f"{provider} cannot list its models; choose one by its ID.",
                                 data={"models": {provider: None}})
        if not listed:
            return ToolResult.ok(f"{provider} lists no models its settings offer (models_since, models_exclude, "
                                 "models_allow).",
                                 data={"models": {provider: []}})
        shown = ", ".join(listed)
        if len(shown) > 1500:
            shown = shown[:1500].rsplit(", ", 1)[0] + f", … ({len(listed)} in all)"
        return ToolResult.ok(f"{provider}: {shown}", data={"models": {provider: listed}})

    async def _choose(self, address: Address, personas: list[Persona], available: list[str], who: str,
                      choice: str) -> ToolResult:
        persona = next((item for item in personas if who.lower() in (item.name.lower(), item.display_name.lower())),
                       None)
        if persona is None:
            names = ", ".join(item.name for item in personas) or "none"
            return ToolResult.fail(f"No persona {who!r} here (here: {names}).", "invalid_arguments")
        if choice.lower() == "reset":
            self.set(str(address), persona.name, None)
            model, _ = self.current(str(address), persona)
            return ToolResult.ok(f"{persona.display_name} answers with her own model here again ({model}).",
                                 data={"persona": persona.name, "model": model, "chosen": False})
        wanted = candidate(choice)
        if not wanted["model"] or wanted["provider"] not in available:
            return ToolResult.fail(f"Give the model as <provider>/<model>, with a provider that has a key: "
                                   f"{', '.join(available) or 'none'}.", "invalid_arguments")
        # Only what the lists offer (the provider's models_since, models_exclude and models_allow); `reset`
        # goes back to her own models.
        models, errors = await self.catalogue([wanted["provider"]])
        if wanted["provider"] in errors:
            return ToolResult.fail(f"Could not check {wanted['provider']}'s models ({errors[wanted['provider']]}); "
                                   "try again.", "unavailable")
        listed = models.get(wanted["provider"])
        if listed is not None and wanted["model"] not in listed:
            return ToolResult.fail(f"{wanted['provider']}/{wanted['model']} is not offered here; "
                                   f"`$model {wanted['provider']}` lists the ones that are.", "invalid_arguments")
        self.set(str(address), persona.name, choice)
        note = "" if listed is not None else " (its provider cannot list its models; it is tried as given)"
        return ToolResult.ok(f"{persona.display_name} answers with {choice} here{note}; her own models stay as the "
                             "fallback.", data={"persona": persona.name, "model": choice, "chosen": True})


def model_arguments_tier(arguments: dict[str, Any]):
    """Looking is a read; choosing or resetting changes what this conversation uses."""
    words = str(arguments.get("text") or "").split()
    return Risk.WRITE if len(words) == 2 else None

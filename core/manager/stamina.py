"""Stamina: what each persona may spend each day, the account she spends from, and runs that wait when a limit is
reached (contracts 1.12).

    a model answers ──► its cost (the core prices every answer: core/providers/prices.py) ──► today's spend for her
                        (a day starts at midnight in `timezone`), her account's spend, and the run's (usage `cost`)
    before a model call ──► today's spend ≥ her allowance (with what you added today)? or the run's spend ≥ its budget
                        (Request.extra `budget_usd`, with what you added)? ──► the run waits for the owner (`Paused`):
                        an approval card, `core.stamina` ("DeepSeek has spent her $0.50 for today. Let her spend
                        $0.50 more?"); approved, the limit grows by `step` and the run carries on; denied, it stops
    the pages ──► InterfacePort.stamina(persona): {today: {spent, allowance, left, day}, account: {provider, kind,
                  currency, total, left, as_of}}

Settings (core.yml `stamina:`): `allowance` (dollars a day for each persona, default 0.50; 0: no daily limit),
`personas` ({name: allowance}), `step` (what one approval adds; default the allowance, else $0.50), `timezone`
(default this machine's), `accounts` ({provider: {balance: true}} for an account its provider reports (DeepSeek's), or
{provider: {budget: 10.0, since: "2026-10-27"}} for a budget you enter, counting down from what is spent since),
`balance_every_min` (10). A watcher's calls (a memory learning) are counted, never stopped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.providers.prices import get_json

DEFAULT_ALLOWANCE = 0.50
BALANCES = {"deepseek": "https://api.deepseek.com/user/balance"}  # providers that report an account's balance


class Paused(BaseException):
    """A run reached a spending limit and waits for the owner. A BaseException, so engines' own error handling
    (`except Exception`) lets it through to the core, which parks the run until the approval is decided."""

    def __init__(self, approval_id: str = ""):
        super().__init__(approval_id)
        self.approval_id = approval_id


class Stamina:
    def __init__(self, tank, settings: Mapping[str, Any] | None, providers, personas: Callable[[], Mapping[str, Any]],
                 clock: Callable[[], float] = time.time):
        self.settings = dict(settings or {})
        self.store = tank.store("core:stamina")
        self.providers, self.personas, self.clock = providers, personas, clock
        try:
            self.zone = ZoneInfo(str(self.settings["timezone"])) if self.settings.get("timezone") else None
        except (ZoneInfoNotFoundError, ValueError):
            self.zone = None
        self.accounts = {str(name): dict(value) for name, value in dict(self.settings.get("accounts") or {}).items()
                         if isinstance(value, Mapping)}
        self._balances_task: asyncio.Task | None = None

    # ---- limits ------------------------------------------------------------------------------------------------
    def day(self) -> str:
        return dt.datetime.fromtimestamp(self.clock(), self.zone).date().isoformat()

    def allowance(self, persona: str) -> float:
        """Her daily allowance in dollars (0: no daily limit)."""
        own = dict(self.settings.get("personas") or {})
        value = own.get(persona, self.settings.get("allowance", DEFAULT_ALLOWANCE))
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return DEFAULT_ALLOWANCE

    def step(self, persona: str) -> float:
        try:
            given = float(self.settings.get("step") or 0)
        except (TypeError, ValueError):
            given = 0.0
        return given if given > 0 else (self.allowance(persona) or DEFAULT_ALLOWANCE)

    def spent_today(self, persona: str) -> float:
        return float(self.store.get(f"day:{self.day()}:{persona}") or 0.0)

    def added_today(self, persona: str) -> float:
        return float(self.store.get(f"more:{self.day()}:{persona}") or 0.0)

    def run_added(self, run_id: str) -> float:
        return float(self.store.get(f"run:{run_id}") or 0.0)

    def check(self, record, persona: str | None, budget: float | None) -> dict | None:
        """Why this run must wait before its next model call, or None: her day's allowance spent, or the run's budget."""
        if persona:
            allowance = self.allowance(persona)
            limit = allowance + self.added_today(persona)
            spent = self.spent_today(persona)
            if allowance > 0 and spent >= limit:
                return {"kind": "daily", "persona": persona, "spent": round(spent, 6), "limit": round(limit, 6),
                        "amount": self.step(persona), "day": self.day()}
        if budget:
            limit = float(budget) + self.run_added(record.id)
            spent = float(record.usage.get("cost", 0.0))
            if spent >= limit:
                return {"kind": "session", "persona": persona or "", "spent": round(spent, 6), "limit": round(limit, 6),
                        "amount": round(float(budget), 4), "run": record.id}
        return None

    def extend(self, arguments: Mapping[str, Any], spent_now: float | None = None) -> str:
        """Grow the limit a run stopped at (approved by the owner), so that `amount` more may be spent from what is
        spent now (one answer may have gone past the limit by more than a step): her allowance for that day, or the
        run's budget. `spent_now`: the run's spend, for a session."""
        amount = max(0.0, float(arguments.get("amount") or 0))
        if arguments.get("kind") == "session":
            key = f"run:{arguments.get('run')}"
            budget = float(arguments.get("limit") or 0) - float(self.store.get(key) or 0.0)  # the budget it was given
            spent = float(spent_now if spent_now is not None else arguments.get("spent") or 0)
            self.store.set(key, round(max(float(self.store.get(key) or 0.0) + amount, spent + amount - budget), 6))
            return f"The session may spend {money(amount)} more."
        persona, day = str(arguments.get("persona") or ""), str(arguments.get("day") or self.day())
        key = f"more:{day}:{persona}"
        spent = float(self.store.get(f"day:{day}:{persona}") or 0.0)
        added = max(float(self.store.get(key) or 0.0) + amount, spent + amount - self.allowance(persona))
        self.store.set(key, round(added, 6))
        return f"{self.name(persona)} may spend {money(amount)} more today."

    # ---- the ledger --------------------------------------------------------------------------------------------
    def record(self, persona: str | None, provider: str, cost: float | None) -> None:
        """One answer's cost: on her day, and on her account's provider."""
        if not cost:
            return
        if persona:
            key = f"day:{self.day()}:{persona}"
            self.store.set(key, round(float(self.store.get(key) or 0.0) + float(cost), 6))
        if provider:
            key = f"account:{provider}"
            self.store.set(key, round(float(self.store.get(key) or 0.0) + float(cost), 6))
            self._spent_on(provider, float(cost))

    def _spent_on(self, provider: str, cost: float) -> None:
        """What a budgeted account spent since its budget's date (`since`): kept per day, summed from that day."""
        key = f"spent:{provider}:{self.day()}"
        self.store.set(key, round(float(self.store.get(key) or 0.0) + cost, 6))

    def spent_since(self, provider: str, since: str) -> float:
        return round(sum(float(self.store.get(key) or 0.0) for key in self.store.keys(f"spent:{provider}:")
                         if key.rsplit(":", 1)[-1] >= since), 6)

    # ---- accounts ----------------------------------------------------------------------------------------------
    def provider_of(self, persona: str) -> str:
        """The account she spends from: her own models' provider (her `own` tier, else her profile)."""
        found = self.personas().get(persona)
        if found is None:
            return ""
        profile = found.tier("own") if hasattr(found, "tier") else found.profile
        candidates = self.providers.candidates(profile) or self.providers.candidates(found.profile)
        return str(candidates[0]["provider"]) if candidates else ""

    def account(self, provider: str) -> dict | None:
        """Her account: the balance its provider reported, or the budget you entered less what was spent since."""
        given = self.accounts.get(provider)
        if not provider or given is None:
            return None
        if given.get("balance"):
            balance = self.store.get(f"balance:{provider}")
            if not balance:
                return {"provider": provider, "kind": "balance", "left": None, "currency": "", "as_of": None}
            return {"provider": provider, "kind": "balance", **balance}
        try:
            budget = float(given.get("budget") or 0)
        except (TypeError, ValueError):
            budget = 0.0
        if budget <= 0:
            return None
        since = str(given.get("since") or "")
        spent = self.spent_since(provider, since) if since else float(self.store.get(f"account:{provider}") or 0.0)
        return {"provider": provider, "kind": "budget", "currency": "USD", "total": budget,
                "left": round(max(0.0, budget - spent), 4), "spent": spent, "since": since}

    async def refresh_balances(self) -> None:
        """Ask the providers that report one for their account's balance (DeepSeek's `/user/balance`), with the key
        the provider already uses; a failure keeps the last figure."""
        for provider, given in self.accounts.items():
            url = BALANCES.get(provider)
            if not given.get("balance") or not url:
                continue
            key = self.providers.key(provider)
            if not key:
                continue
            try:
                body = await asyncio.to_thread(get_json, url, {"Authorization": f"Bearer {key}"}, 15.0)
            except Exception:
                continue
            infos = body.get("balance_infos") or []
            if not infos:
                continue
            first = infos[0]
            try:
                total = float(first.get("total_balance"))
            except (TypeError, ValueError):
                continue
            before = self.store.get(f"balance:{provider}") or {}
            self.store.set(f"balance:{provider}", {"currency": str(first.get("currency") or ""), "left": total,
                                                    # the fullest it has been seen: what the pages measure it against
                                                    "peak": max(total, float(before.get("peak") or 0)),
                                                    "available": bool(body.get("is_available", True)),
                                                    "as_of": round(self.clock())})

    def start(self) -> None:
        """Keep the reported balances fresh while the core runs."""
        if not any(given.get("balance") for given in self.accounts.values()) or self._balances_task is not None:
            return
        every = max(1.0, float(self.settings.get("balance_every_min", 10))) * 60

        async def keep():
            while True:
                await self.refresh_balances()
                await asyncio.sleep(every)
        self._balances_task = asyncio.get_running_loop().create_task(keep())

    def stop(self) -> None:
        if self._balances_task is not None:
            self._balances_task.cancel()
            self._balances_task = None

    # ---- what the pages read -----------------------------------------------------------------------------------
    def name(self, persona: str) -> str:
        found = self.personas().get(persona)
        return getattr(found, "display_name", "") or persona

    def view(self, persona: str) -> dict:
        """What a page shows of her (InterfacePort.stamina): today's spend against her allowance, and her account."""
        allowance = self.allowance(persona)
        limit = allowance + self.added_today(persona)
        spent = self.spent_today(persona)
        return {"today": {"spent": round(spent, 4), "allowance": round(limit, 4) if allowance else None,
                          "left": round(max(0.0, limit - spent), 4) if allowance else None, "day": self.day()},
                "account": self.account(self.provider_of(persona))}


def money(dollars: float) -> str:
    return f"${dollars:.2f}" if dollars >= 0.1 else f"${dollars:.3f}" if dollars >= 0.001 or not dollars else f"${dollars:.4f}"

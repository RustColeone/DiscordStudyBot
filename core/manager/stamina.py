"""Stamina: what each persona may spend each day, the account she spends from, and runs that wait when a limit is
reached (contracts 1.12).

    a model answers ──► its cost (the core prices every answer: core/providers/prices.py) ──► today's spend for her
                        (a day starts at midnight in `timezone`), her account's spend, and the run's (usage `cost`)
    before a model call ──► today's spend ≥ her allowance (with what you added today)? or the run's spend ≥ its budget
                        (Request.extra `budget_usd`, with what you added)? ──► the run waits for the owner (`Paused`):
                        an approval card, `core.stamina` ("DeepSeek has spent her $0.50 for today. Let her spend
                        $0.50 more?"); approved, the limit grows by `step` and the run carries on; denied, it stops
    and everyone's together against the day's total (`total`): reached, a run waits the same way
    the pages ──► InterfacePort.stamina(persona): {today: {spent, allowance, left, day}, account: {provider, kind,
                  currency, total, left, as_of}}; with no persona, everyone's day against the total

Settings (core.yml `stamina:`): `total` (dollars a day for everyone together, default 2.00; 0: no total),
`allowance` (dollars a day for each persona, default 0.50; 0: no daily limit), `personas` ({name: allowance}),
`step` (what one approval adds; default the allowance, else $0.50), `timezone` (default this machine's), `accounts`
({provider: {balance: true}} for an account its provider reports (DeepSeek's, OpenRouter's), or {provider: {budget:
10.0, since: "2026-10-27"}} for a budget you enter, counting down from what is spent since), `balance_every_min` (10).
`$stamina` shows the day and changes the total, an allowance or a budget (kept in the core's store, over core.yml). A
watcher's calls (a memory learning) are counted, never stopped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.providers.prices import get_json

DEFAULT_ALLOWANCE = 0.50
DEFAULT_TOTAL = 2.00
# Providers that report an account's balance: where to ask, and how to read the answer (OpenRouter is one account for
# every model, so with it every student spends from the same balance).
BALANCES = {"deepseek": "https://api.deepseek.com/user/balance", "openrouter": "https://openrouter.ai/api/v1/credits"}


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
        """Her daily allowance in dollars (0: no daily limit): what `$stamina` set, else core.yml's."""
        own = dict(self.settings.get("personas") or {})
        value = self.store.get(f"setting:allowance:{persona}")
        if value is None:
            value = own.get(persona, self.store.get("setting:allowance", self.settings.get("allowance", DEFAULT_ALLOWANCE)))
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

    def total(self) -> float:
        """The day's total for everyone together (0: none): what `$stamina` set, else core.yml's."""
        value = self.store.get("setting:total", self.settings.get("total", DEFAULT_TOTAL))
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return DEFAULT_TOTAL

    def spent_total(self) -> float:
        return float(self.store.get(f"total:{self.day()}") or 0.0)

    def added_total(self) -> float:
        return float(self.store.get(f"more-total:{self.day()}") or 0.0)

    def spent_today(self, persona: str) -> float:
        return float(self.store.get(f"day:{self.day()}:{persona}") or 0.0)

    def added_today(self, persona: str) -> float:
        return float(self.store.get(f"more:{self.day()}:{persona}") or 0.0)

    def run_added(self, run_id: str) -> float:
        return float(self.store.get(f"run:{run_id}") or 0.0)

    def check(self, record, persona: str | None, budget: float | None) -> dict | None:
        """Why this run must wait before its next model call, or None: her day's allowance spent, everyone's total
        for the day, or the run's budget."""
        total = self.total()
        if total > 0:
            limit, spent = total + self.added_total(), self.spent_total()
            if spent >= limit:
                return {"kind": "total", "persona": persona or "", "spent": round(spent, 6), "limit": round(limit, 6),
                        "amount": self.step(persona or ""), "day": self.day()}
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
        if arguments.get("kind") == "total":
            day = str(arguments.get("day") or self.day())
            key = f"more-total:{day}"
            spent = float(self.store.get(f"total:{day}") or 0.0)
            self.store.set(key, round(max(float(self.store.get(key) or 0.0) + amount, spent + amount - self.total()), 6))
            return f"Everyone together may spend {money(amount)} more today."
        persona, day = str(arguments.get("persona") or ""), str(arguments.get("day") or self.day())
        key = f"more:{day}:{persona}"
        spent = float(self.store.get(f"day:{day}:{persona}") or 0.0)
        added = max(float(self.store.get(key) or 0.0) + amount, spent + amount - self.allowance(persona))
        self.store.set(key, round(added, 6))
        return f"{self.name(persona)} may spend {money(amount)} more today."

    # ---- the ledger --------------------------------------------------------------------------------------------
    def record(self, persona: str | None, provider: str, cost: float | None) -> None:
        """One answer's cost: on everyone's day, on hers, and on her account's provider."""
        if not cost:
            return
        key = f"total:{self.day()}"
        self.store.set(key, round(float(self.store.get(key) or 0.0) + float(cost), 6))
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
        given = {**(self.accounts.get(provider) or {}), **(self.store.get(f"setting:account:{provider}") or {})}
        if not provider or not given:
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
            read = read_balance(provider, body)
            if read is None:
                continue
            total, currency = read
            before = self.store.get(f"balance:{provider}") or {}
            self.store.set(f"balance:{provider}", {"currency": currency, "left": total,
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

    def view_total(self) -> dict:
        """Everyone's day against the total (InterfacePort.stamina with no persona)."""
        total = self.total()
        limit, spent = total + self.added_total(), self.spent_total()
        return {"today": {"spent": round(spent, 4), "allowance": round(limit, 4) if total else None,
                          "left": round(max(0.0, limit - spent), 4) if total else None, "day": self.day()}}

    def history(self, days: int = 14) -> list[dict]:
        """Spending by day, newest last, for the last `days` days (today included): everyone's total and each
        persona's (InterfacePort.spending)."""
        today = dt.datetime.fromtimestamp(self.clock(), self.zone).date()
        wanted = [(today - dt.timedelta(days=back)).isoformat() for back in range(max(1, int(days)) - 1, -1, -1)]
        found = {day: {"day": day, "total": float(self.store.get(f"total:{day}") or 0.0), "personas": {}} for day in wanted}
        for key in self.store.keys("day:"):
            _, day, persona = key.split(":", 2)
            if day in found:
                found[day]["personas"][persona] = round(float(self.store.get(key) or 0.0), 6)
        for entry in found.values():  # (a day from before the total was kept has only its personas')
            entry["total"] = round(max(entry["total"], sum(entry["personas"].values())), 6)
        return [found[day] for day in wanted]

    # ---- $stamina: the day, and what you change -------------------------------------------------------------------
    def command(self, text: str) -> str:
        """`$stamina` (the day), `$stamina total 3` (everyone's day), `$stamina <persona> 0.8` (her day; `reset`
        goes back to core.yml's), `$stamina budget <provider> 20` (an account's budget, counted from today)."""
        words = str(text or "").split()
        if not words:
            return self.report()
        try:
            if words[0] == "total" and len(words) == 2:
                self.store.set("setting:total", max(0.0, float(words[1])))
                return f"Everyone's day: {money(self.total())} ({money(self.spent_total())} spent so far)."
            if words[0] == "budget" and len(words) == 3:
                self.store.set(f"setting:account:{words[1]}", {"budget": max(0.0, float(words[2])), "since": self.day()})
                return f"{words[1]}: a {money(float(words[2]))} budget, counted from today."
            if len(words) == 2 and words[0] in self.personas():
                if words[1] == "reset":
                    self.store.delete(f"setting:allowance:{words[0]}")
                else:
                    self.store.set(f"setting:allowance:{words[0]}", max(0.0, float(words[1])))
                return f"{self.name(words[0])}'s day: {money(self.allowance(words[0]))}."
        except ValueError:
            pass
        return ("Usage: $stamina · $stamina total <dollars> · $stamina <persona> <dollars>|reset · "
                "$stamina budget <provider> <dollars>")

    def report(self) -> str:
        total = self.view_total()["today"]
        lines = [f"Today ({total['day']}): {money(total['spent'])} spent"
                 + (f" of {money(total['allowance'])} for everyone" if total["allowance"] else " (no total set)")]
        for persona in self.personas():
            view = self.view(persona)
            today, account = view["today"], view["account"]
            line = f"{self.name(persona)}: {money(today['spent'])}" + (f" of {money(today['allowance'])}" if today["allowance"] else "")
            if account and account.get("left") is not None:
                line += f" · {account['provider']} {account['left']} {account.get('currency') or ''}".rstrip()
            lines.append(line)
        return "\n".join(lines)

    def view(self, persona: str) -> dict:
        """What a page shows of her (InterfacePort.stamina): today's spend against her allowance, and her account."""
        allowance = self.allowance(persona)
        limit = allowance + self.added_today(persona)
        spent = self.spent_today(persona)
        return {"today": {"spent": round(spent, 4), "allowance": round(limit, 4) if allowance else None,
                          "left": round(max(0.0, limit - spent), 4) if allowance else None, "day": self.day()},
                "account": self.account(self.provider_of(persona))}


def read_balance(provider: str, body: Mapping[str, Any]) -> tuple[float, str] | None:
    """A provider's balance answer as (amount, currency): DeepSeek's `balance_infos`, OpenRouter's credits less usage."""
    try:
        if provider == "openrouter":
            data = body.get("data") or {}
            return round(float(data["total_credits"]) - float(data.get("total_usage") or 0), 4), "USD"
        first = (body.get("balance_infos") or [None])[0]
        return (float(first["total_balance"]), str(first.get("currency") or "")) if first else None
    except (KeyError, TypeError, ValueError):
        return None


def money(dollars: float) -> str:
    return f"${dollars:.2f}" if dollars >= 0.1 else f"${dollars:.3f}" if dollars >= 0.001 or not dollars else f"${dollars:.4f}"

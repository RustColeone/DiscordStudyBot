"""Model prices, so every model answer carries what it cost (contracts 1.12): Artificial Analysis's list, fetched
again once a day old, under the owner's own figures.

    the core starts, or a model answers ──► the kept list is over 24 h old (and a key is set)? ──► fetch every page of
                      /api/v2/language/models/free in the background (a failure keeps the last good list; next try in
                      an hour)
    a model answers ──► its price: core.yml `prices: {models}` first, then `aliases` (→ an Artificial Analysis slug),
                        then a match by name (exact; the same words in another order; else the one closest model of the
                        same maker; ambiguous → unknown, never a guess) ──► cost = tokens × list price (cache hits at
                        the listed cache price, else the input price), on the answer

Settings (core.yml `prices:`): `models` ({"provider/model" or "provider": {input, output, cache_hit}}), `aliases`
({"provider/model": slug}), `key_env` (ARTIFICIAL_ANALYSIS_API_KEY), `url`, `max_age_h` (24). The free list needs a
free key from artificialanalysis.ai; their terms ask that the source be credited wherever the data is shown, and the
pages do. Costs are estimates from list prices.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping


SOURCE = "https://artificialanalysis.ai/api/v2/language/models/free"
ATTRIBUTION = {"name": "Artificial Analysis", "url": "https://artificialanalysis.ai"}
RETRY_S = 3600.0
# The makers Artificial Analysis names for each provider's models (matched loosely, lowercase, letters and digits).
CREATORS = {"deepseek": ["deepseek"], "openai": ["openai"], "claude": ["anthropic"], "gemini": ["google"],
            "grok": ["xai"]}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")


def words(text: str) -> list[str]:
    return [word for word in norm(text).split("-") if word]


class PriceBook:
    def __init__(self, path: Path, settings: Mapping[str, Any] | None = None, clock: Callable[[], float] = time.time):
        settings = dict(settings or {})
        self.path, self.clock = Path(path), clock
        self.own = {str(key): dict(value) for key, value in dict(settings.get("models") or {}).items()
                    if isinstance(value, Mapping)}
        self.aliases = {str(key): str(value) for key, value in dict(settings.get("aliases") or {}).items()}
        self.url = str(settings.get("url") or SOURCE)
        self.key_env = str(settings.get("key_env") or "ARTIFICIAL_ANALYSIS_API_KEY")
        self.max_age_s = float(settings.get("max_age_h", 24)) * 3600
        self._lock = asyncio.Lock()
        self.cache: dict = self._load()

    # ---- the kept list ----------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        scratch = self.path.with_suffix(".tmp")
        scratch.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        scratch.replace(self.path)

    @property
    def key(self) -> str:
        return os.getenv(self.key_env, "").strip().strip("\"'")

    def age_s(self) -> float | None:
        fetched = self.cache.get("fetched_at")
        return None if not fetched else max(0.0, self.clock() - float(fetched))

    def stale(self) -> bool:
        age = self.age_s()
        return age is None or age > self.max_age_s

    async def refresh_if_stale(self, timeout_s: float = 20.0) -> str:
        """On use: fetch the list again when the kept one is a day old (or missing). Returns `fresh`, `fetched`,
        `no_key`, `waiting` (it failed less than an hour ago) or `failed`. Never raises: prices only label work."""
        if not self.stale():
            return "fresh"
        if not self.key:
            return "no_key"
        if self.cache.get("error") and self.clock() - float(self.cache.get("tried_at") or 0) < RETRY_S:
            return "waiting"
        async with self._lock:
            if not self.stale():
                return "fresh"
            try:
                models = await asyncio.wait_for(self._fetch(), timeout_s)
            except Exception as error:
                self.cache.update(error=f"{type(error).__name__}: {error}"[:300], tried_at=self.clock())
                self._save()
                return "failed"
            self.cache = {"fetched_at": self.clock(), "tried_at": self.clock(), "error": None, "source": self.url,
                          "models": models}
            self._save()
            return "fetched"

    async def _fetch(self) -> list[dict]:
        models: list[dict] = []
        for page in range(1, 51):
            body = await asyncio.to_thread(get_json, f"{self.url}?{urllib.parse.urlencode({'page': page})}",
                                           {"x-api-key": self.key})
            models.extend(row for row in map(self._row, body.get("data") or ()) if row)
            if not (body.get("pagination") or {}).get("has_more"):
                break
        if not models:
            raise RuntimeError("the list came back empty")
        return models

    @staticmethod
    def _row(item: Mapping[str, Any]) -> dict | None:
        pricing = item.get("pricing") or item
        price_in, price_out = pricing.get("price_1m_input_tokens"), pricing.get("price_1m_output_tokens")
        if price_in is None or price_out is None:
            return None  # not measured, which never means free
        return {"slug": str(item.get("slug") or ""), "name": str(item.get("name") or ""),
                "creator": str((item.get("model_creator") or {}).get("name") or ""), "input": float(price_in),
                "output": float(price_out), "cache_hit": pricing.get("price_1m_cache_hit_tokens"),
                "released": str(item.get("release_date") or "")}

    # ---- prices ---------------------------------------------------------------------------------------
    def price(self, provider: str, model: str) -> dict | None:
        """Dollars per million input and output tokens for one model, with where the figure came from."""
        mine = self.own.get(f"{provider}/{model}") or self.own.get(provider)
        if mine:
            return {"input": float(mine.get("input", 0)), "output": float(mine.get("output", 0)),
                    "cache_hit": float(mine["cache_hit"]) if mine.get("cache_hit") is not None else None,
                    "source": "config"}
        entry = self.match(provider, model)
        if entry is None:
            return None
        fetched = self.cache.get("fetched_at")
        return {"input": entry["input"], "output": entry["output"],
                "cache_hit": float(entry["cache_hit"]) if entry.get("cache_hit") is not None else None,
                "source": "artificial-analysis", "slug": entry["slug"],
                "as_of": time.strftime("%Y-%m-%d", time.localtime(float(fetched))) if fetched else ""}

    def match(self, provider: str, model: str) -> dict | None:
        models = self.cache.get("models") or []
        alias = self.aliases.get(f"{provider}/{model}")
        if alias:
            return next((entry for entry in models if norm(entry["slug"]) == norm(alias)), None)
        exact = [entry for entry in models if norm(entry["slug"]) == norm(model)]
        if len(exact) == 1:
            return exact[0]
        wanted = words(model)
        reordered = [entry for entry in models if sorted(words(entry["slug"])) == sorted(wanted)]
        if len(reordered) == 1:
            return reordered[0]
        ranked = self.candidates(provider, model)
        if not ranked:
            return None
        fewest = ranked[0][0]
        best = [entry for extra, entry in ranked if extra == fewest]
        return best[0] if len(best) == 1 else None

    def candidates(self, provider: str, model: str, limit: int = 50) -> list[tuple[int, dict]]:
        """Models of the same maker whose name holds every word of `model`, fewest extra words first."""
        makers = CREATORS.get(provider)
        if not makers:
            return []
        wanted = set(words(model))
        found = []
        for entry in self.cache.get("models") or ():
            if not any(maker in norm(entry["creator"]).replace("-", "") for maker in makers):
                continue
            have = set(words(entry["slug"]))
            if wanted <= have:
                found.append((len(have - wanted), entry))
        return sorted(found, key=lambda pair: pair[0])[:limit]

    @staticmethod
    def cost(price: Mapping[str, Any] | None, input_tokens: int, output_tokens: int,
             cached_input_tokens: int = 0) -> float | None:
        """Dollars for one answer: fresh input at the input price, cache hits at the cache price when one is listed
        (else at the input price), output at the output price."""
        if not price:
            return None
        cached = max(0, min(int(cached_input_tokens or 0), int(input_tokens or 0)))
        hit = price.get("cache_hit")
        hit = float(price["input"]) if hit is None else float(hit)
        return round(((int(input_tokens or 0) - cached) * float(price["input"]) + cached * hit
                      + int(output_tokens or 0) * float(price["output"])) / 1e6, 6)

    def status(self, models=()) -> dict:
        """For a page: where prices come from, how old they are, and the price of each of `models` (rows with a
        `provider` and a `model`, given back with `price` and, when it is unknown, the closest `candidates`)."""
        fetched = self.cache.get("fetched_at")
        rows = []
        for row in models or ():
            provider, model = str(row.get("provider") or ""), str(row.get("model") or "")
            if not provider or any(item["provider"] == provider and item["model"] == model for item in rows):
                continue
            price = self.price(provider, model)
            rows.append({**dict(row), "provider": provider, "model": model, "price": price, "candidates": [] if price
                         else [entry["slug"] for _, entry in self.candidates(provider, model, 3)]})
        return {"attribution": ATTRIBUTION, "fetched_at": fetched, "stale": self.stale(), "key": bool(self.key),
                "key_env": self.key_env, "error": self.cache.get("error"), "count": len(self.cache.get("models") or ()),
                "models": rows}


def get_json(url: str, headers: Mapping[str, str], timeout_s: float = 20.0) -> dict:
    """GET a JSON document (the standard library: the core carries no HTTP client of its own); raises on failure."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:200]}") from None

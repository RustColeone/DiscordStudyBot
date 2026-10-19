"""The Providers block: named model profiles with ordered fallback candidates."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from typing import Any, Callable, Mapping

import jsonschema

from contracts import ModelError, ModelRequest, ModelResult
from core.providers.adapters import ADAPTERS, NotConfigured, api_key, load_images

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json(text: str, schema: Mapping[str, Any]) -> Any:
    match = FENCE.match(text or "")
    body = match.group(1) if match else (text or "").strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise ModelError("invalid_output", f"The model did not return valid JSON: {error.msg}", text) from None
    try:
        jsonschema.validate(value, schema)
    except jsonschema.ValidationError as error:
        path = "/".join(str(part) for part in error.absolute_path) or "(root)"
        raise ModelError("invalid_output", f"The model output does not match the schema at {path}: {error.message}",
                         text) from None
    return value


def released_row(item) -> tuple[str, dt.date | None]:
    """A listed model: an ID, or an (ID, release date or datetime) pair."""
    if isinstance(item, (tuple, list)):
        model, released = item
        return str(model), (released.date() if isinstance(released, dt.datetime) else released)
    return str(item), None


def offered(settings: Mapping[str, Any], model: str, released: dt.date | None) -> bool:
    """Released since `models_since` (when the provider gives dates), matching none of `models_exclude` and, if
    `models_allow` is given, one of it. Patterns are regular expressions searched in the ID, ignoring case, so a
    plain word works too."""
    since = settings.get("models_since")  # YAML reads 2026-06-01 as a date; Python settings may give a string
    if since and released is not None and released < dt.date.fromisoformat(str(since)):
        return False
    if matches(settings.get("models_exclude"), model):
        return False
    allow = settings.get("models_allow")
    return not allow or matches(allow, model)


def matches(patterns, model: str) -> bool:
    return any(re.search(str(pattern), model, re.IGNORECASE) for pattern in patterns or ())


class Providers:
    def __init__(self, providers: Mapping[str, Mapping[str, Any]], profiles: Mapping[str, list[Mapping[str, Any]]],
                 adapters: Mapping[str, Any] | None = None, record: Callable[..., None] | None = None):
        self._settings = {name: dict(value) for name, value in (providers or {}).items()}
        self._profiles = {name: [dict(candidate) for candidate in candidates]
                          for name, candidates in (profiles or {}).items()}
        self._adapters = dict(adapters or {})
        self._kinds = dict(ADAPTERS)  # provider kind → adapter factory: the built-in ones, then modules'
        self._record = record or (lambda *args, **kwargs: None)
        self._spacing: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}
        self._model_lists: dict[str, tuple[float, list]] = {}  # provider → (fetched at, [(ID, released)])
        for name, candidates in self._profiles.items():
            for candidate in candidates:
                if candidate.get("provider") not in self._settings and candidate.get("provider") not in self._adapters:
                    raise ValueError(f"Profile {name!r} uses unknown provider {candidate.get('provider')!r}")

    def add_kind(self, kind: str, factory) -> None:
        """A provider kind a module adds (contracts.AdapterFactory); an existing kind is never replaced."""
        if kind in self._kinds:
            raise ValueError(f"Provider kind {kind!r} already exists")
        if not callable(factory):
            raise ValueError(f"Provider kind {kind!r} needs an adapter factory")
        self._kinds[kind] = factory

    def add_provider(self, name: str, settings: Mapping[str, Any]) -> None:
        """A provider an extension suggests; settings already configured (core.yml) win key by key."""
        self._settings[name] = {**dict(settings), **self._settings.get(name, {})}

    def key_variables(self) -> set[str]:
        """The environment variables holding the providers' API keys."""
        return {str(settings["api_key_env"]) for settings in self._settings.values() if settings.get("api_key_env")}

    def add_profile(self, name: str, candidates) -> None:
        candidates = [dict(candidate) for candidate in candidates]
        for candidate in candidates:
            if candidate.get("provider") not in self._settings and candidate.get("provider") not in self._adapters:
                raise ValueError(f"Profile {name!r} uses unknown provider {candidate.get('provider')!r}")
        self._profiles.setdefault(name, candidates)

    def profiles(self) -> list[str]:
        return sorted(self._profiles)

    def configured(self) -> list[str]:
        """Providers used by some profile that can be called: injected, with their key present, or of a module's
        kind that names no key (a local server, say; the built-in kinds always need one)."""
        used = {candidate["provider"] for candidates in self._profiles.values() for candidate in candidates}
        return sorted(name for name in used if self.ready(name))

    def ready(self, name: str) -> bool:
        """Can this provider be called (see configured)?"""
        settings = self._settings.get(name, {})
        if name in self._adapters:
            return True
        try:
            api_key(settings)  # the same test the built-in adapters make: `disabled` and the like count as unset
            return True
        except NotConfigured:
            pass
        kind = settings.get("kind")
        return kind in self._kinds and kind not in ADAPTERS and not settings.get("api_key_env")

    def available(self) -> list[str]:
        """Every provider that can be called, used by a profile or not."""
        return sorted(name for name in {*self._settings, *self._adapters} if self.ready(name))

    def candidates(self, profile: str) -> list[dict]:
        return [dict(candidate) for candidate in self._profiles.get(profile, [])]

    def known(self, name: str) -> bool:
        return name in self._settings or name in self._adapters

    async def list_models(self, name: str, max_age_s: float = 3600.0) -> list[str] | None:
        """The model IDs a provider offers, asked live and kept for an hour; None when its adapter cannot list
        them. Raises NotConfigured for a provider that cannot be called. The provider's settings narrow the list
        to what is worth offering: `models_since` (a date; models released before it go, undated ones stay),
        `models_exclude` (patterns; IDs matching one go) and `models_allow` (patterns; if given, IDs must match one).
        Only these can be chosen with $model."""
        if not self.ready(name):
            raise NotConfigured(f"Provider {name!r} has no key")
        cached = self._model_lists.get(name)
        if cached is not None and time.monotonic() - cached[0] < max_age_s:
            rows = cached[1]
        else:
            lister = getattr(self._adapter(name), "list_models", None)
            if lister is None:
                return None
            rows = [released_row(item) for item in await lister()]
            self._model_lists[name] = (time.monotonic(), rows)
        settings = self._settings.get(name, {})
        return sorted({model for model, released in rows if offered(settings, model, released)})

    def _sees(self, candidate: Mapping[str, Any]) -> bool:
        return bool(candidate.get("vision", self._settings.get(candidate["provider"], {}).get("vision", True)))

    async def _space(self, provider: str) -> None:
        """Keep at least `min_interval_s` between requests to one provider (Gemini rate limits)."""
        interval = float(self._settings.get(provider, {}).get("min_interval_s") or 0)
        if interval <= 0:
            return
        lock = self._spacing.setdefault(provider, asyncio.Lock())
        async with lock:
            wait = self._last_request.get(provider, 0.0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[provider] = time.monotonic()

    def _adapter(self, name: str):
        if name not in self._adapters:
            settings = self._settings[name]
            kind = settings.get("kind")
            if kind not in self._kinds:
                raise NotConfigured(f"Provider {name!r} has unknown kind {kind!r}")
            self._adapters[name] = self._kinds[kind](settings)
        return self._adapters[name]

    async def generate(self, request: ModelRequest, profile: str | None = None,
                       first: Mapping[str, Any] | None = None) -> ModelResult:
        """`first`: a candidate tried before the profile's own (a model chosen with $model); the profile's
        candidates stay as the fallback."""
        name = profile or request.profile or "default"
        candidates = self._profiles.get(name)
        if not candidates:
            raise ModelError("not_configured", f"Model profile {name!r} is not configured")
        if first is not None and self.known(first["provider"]):
            candidates = [dict(first), *(candidate for candidate in candidates
                                         if (candidate["provider"], candidate["model"])
                                         != (first["provider"], first["model"]))]
        failures: list[str] = []
        invalid: ModelError | None = None
        images = await asyncio.to_thread(load_images, request.images) if request.images else []
        attempts = [(candidate, bool(images) and self._sees(candidate)) for candidate in candidates]
        # Candidates that can see the images go first; the others get a note instead of the images.
        attempts.sort(key=lambda attempt: not attempt[1] and bool(images))
        for candidate, with_images in attempts:
            provider, model = candidate["provider"], candidate["model"]
            messages = [dict(m) for m in request.messages]
            if images and not with_images and messages and messages[-1]["role"] == "user":
                messages[-1]["content"] += (f"\n\n[{len(images)} image(s) attached, but you cannot see images; "
                                            "say so if they matter.]")
            started = time.monotonic()
            try:
                adapter = self._adapter(provider)
                await self._space(provider)
                extra = {"images": images} if with_images else {}
                result = await asyncio.wait_for(
                    adapter.complete(model, messages, request.output_schema,
                                     request.schema_name, candidate.get("effort") or request.effort,
                                     int(candidate.get("max_tokens") or request.max_tokens), request.timeout_s,
                                     **extra),
                    timeout=request.timeout_s + 5)
            except NotConfigured as error:
                failures.append(f"{provider}: not configured ({error})")
                continue
            except Exception as error:
                failures.append(f"{provider}/{model}: {type(error).__name__}")
                self._record("provider_failed", profile=name, provider=provider, model=model,
                             error=type(error).__name__, duration_s=round(time.monotonic() - started, 3))
                continue
            duration = round(time.monotonic() - started, 3)
            self._record("provider_call", profile=name, provider=provider, model=model, duration_s=duration,
                         input_tokens=result.input_tokens, output_tokens=result.output_tokens)
            data = None
            if result.truncated:
                invalid = ModelError("invalid_output", "The model output was truncated", result.text)
                failures.append(f"{provider}/{model}: truncated")
                continue
            if request.output_schema is not None:
                try:
                    data = parse_json(result.text, request.output_schema)
                except ModelError as error:
                    invalid = error
                    failures.append(f"{provider}/{model}: invalid output")
                    continue
            return ModelResult(text=result.text, data=data, provider=provider, model=model,
                               input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                               duration_s=duration, schema_mode=result.schema_mode)
        if invalid is not None:
            raise invalid
        kind = "not_configured" if failures and all("not configured" in item for item in failures) else "unavailable"
        raise ModelError(kind, "No model candidate answered: " + "; ".join(failures))

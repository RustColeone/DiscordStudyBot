"""The Providers block: named model profiles with ordered fallback candidates."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
from typing import Any, Callable, Mapping, Sequence

import jsonschema

from contracts import ModelError, ModelRequest, ModelResult
from core.providers.adapters import ADAPTERS, NotConfigured, api_key, load_images

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)
RETRY_DELAYS_S = (2.0, 8.0)  # a momentary failure: the same model again after these pauses, then the next candidate
MAX_RETRY_AFTER_S = 30.0


def transient(error: Exception) -> bool:
    """A rate limit, a failing or overloaded server, or a dropped connection: worth asking the same model again.
    Refusals (no balance, a bad request, a wrong key) and timeouts, which already waited their time, are not."""
    status = getattr(error, "status_code", None)
    if status is None and isinstance(getattr(error, "code", None), int):
        status = error.code  # Gemini's errors
    if isinstance(status, int):
        return status in (408, 409, 429) or status >= 500
    return type(error).__name__ in ("APIConnectionError", "RateLimitError", "InternalServerError", "OverloadedError")


def retry_after(error: Exception) -> float | None:
    """The pause the provider asked for (`Retry-After`, in seconds), if it said."""
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    try:
        return min(float(headers.get("retry-after")), MAX_RETRY_AFTER_S) if headers.get("retry-after") else None
    except (TypeError, ValueError):
        return None


def check_calls(result, tools) -> None:
    """A tool-calling answer is usable when it says or calls something, and every call names one of the tools
    with arguments matching its `parameters`."""
    specs = {tool.name: tool for tool in tools}
    if not result.tool_calls and not (result.text or "").strip():
        raise ModelError("invalid_output", "The model gave a blank answer (no words, no calls)", result.text)
    for call in result.tool_calls:
        if call.name not in specs:
            raise ModelError("invalid_output", f"The model called {call.name!r}, which is not one of its tools",
                             result.text)
        if not isinstance(call.arguments, Mapping):
            raise ModelError("invalid_output", f"The call to {call.name} has arguments that are not a JSON object",
                             result.text)
        try:
            jsonschema.validate(dict(call.arguments), specs[call.name].parameters)
        except jsonschema.SchemaError:
            continue  # the tool's own parameters are no valid schema: its call is checked where it runs, as ever
        except jsonschema.ValidationError as error:
            path = "/".join(str(part) for part in error.absolute_path) or "(arguments)"
            raise ModelError("invalid_output", f"The call to {call.name} does not match its parameters at {path}: "
                             f"{error.message}", result.text) from None


def replay_for(provider: str, messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Messages for one provider: an assistant message's `replay` goes back only to the provider that wrote it
    (another would get a stranger's reasoning, or refuse it), as that provider's own fields."""
    sent = []
    for message in messages:
        message = dict(message)
        replay = message.pop("replay", None)
        if isinstance(replay, Mapping) and replay.get("provider") == provider and isinstance(replay.get("fields"), Mapping):
            message["replay"] = dict(replay["fields"])
        sent.append(message)
    return sent


def settle_tool_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Messages as a tool-calling API accepts them: each assistant message with `tool_calls` followed at once by one
    `tool` message per call, in the calls' order (anything else that came between them moves after them), a call
    with no result answered "did not run", and a `tool` message that answers no call dropped. Engines may write
    their transcript in the order things happened; the APIs refuse a request otherwise (OpenAI and DeepSeek: 400)."""
    messages = [dict(message) for message in messages]
    results: dict[str, dict] = {}
    for message in messages:
        if message.get("role") == "tool" and message.get("tool_call_id"):
            results.setdefault(str(message["tool_call_id"]), message)
    settled: list[dict] = []
    for message in messages:
        if message.get("role") == "tool":
            continue  # placed after its call below (or dropped, when it answers no call)
        settled.append(message)
        for call in message.get("tool_calls") or () if message.get("role") == "assistant" else ():
            answer = results.pop(str(call.get("id")), None)
            settled.append(answer or {"role": "tool", "tool_call_id": str(call.get("id")),
                                      "content": f"[tool {call.get('name')}] This call did not run."})
    return settled


def parse_json(text: str, schema: Mapping[str, Any]) -> Any:
    match = FENCE.match(text or "")
    body = match.group(1) if match else (text or "").strip()
    try:
        values = [json.loads(body)]
    except json.JSONDecodeError as error:
        # A complete object followed by more (a second object, a sentence): every object in it is a candidate, and
        # the last one that matches the schema is the answer (a model that writes two usually corrects the first).
        values = _objects(body) if error.msg == "Extra data" else []
        if not values:
            raise ModelError("invalid_output", f"The model did not return valid JSON: {error.msg}", text) from None
    problem = None
    for value in reversed(values):
        try:
            jsonschema.validate(value, schema)
            return value
        except jsonschema.ValidationError as error:
            problem = problem or error
    path = "/".join(str(part) for part in problem.absolute_path) or "(root)"
    raise ModelError("invalid_output", f"The model output does not match the schema at {path}: {problem.message}",
                     text) from None


def _objects(body: str) -> list:
    """The JSON values in `body`, one after another (text between them skipped)."""
    decoder, found, index = json.JSONDecoder(), [], 0
    while index < len(body):
        start = min((position for position in (body.find("{", index), body.find("[", index)) if position >= 0),
                    default=-1)
        if start < 0:
            break
        try:
            value, index = decoder.raw_decode(body, start)
            found.append(value)
        except json.JSONDecodeError:
            index = start + 1
    return found


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
                 adapters: Mapping[str, Any] | None = None, record: Callable[..., None] | None = None,
                 configured: Mapping[str, Mapping[str, Any]] | None = None):
        self._settings = {name: dict(value) for name, value in (providers or {}).items()}
        # The owner's own settings (core.yml), which a module's suggestion never overrides; the rest of `providers`
        # (the built-in ones) it does. Without `configured`, all of `providers` counts as the owner's.
        owners = providers if configured is None else configured
        self._configured = {name: dict(value) for name, value in (owners or {}).items()}
        self._profiles = {name: [dict(candidate) for candidate in candidates]
                          for name, candidates in (profiles or {}).items()}
        self._adapters = dict(adapters or {})
        self._given = set(self._adapters)  # adapters handed in (tests): ready by themselves, unlike ones made on use
        self._kinds = dict(ADAPTERS)  # provider kind → adapter factory: the built-in ones, then modules'
        self._record = record or (lambda *args, **kwargs: None)
        self.prices = None  # the price book (prices.py), set by the core: every answer carries its price and cost
        self._refreshing: asyncio.Task | None = None
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
        """A provider an extension suggests: over the built-in settings, under core.yml's, key by key."""
        self._settings[name] = {**self._settings.get(name, {}), **dict(settings), **self._configured.get(name, {})}

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

    def key(self, name: str) -> str:
        """The provider's API key, as its adapter would read it ("" when none is set): for other calls to the same
        account (stamina asks DeepSeek for its balance)."""
        try:
            return api_key(self._settings.get(name, {}))
        except NotConfigured:
            return ""

    def ready(self, name: str) -> bool:
        """Can this provider be called (see configured)?"""
        settings = self._settings.get(name, {})
        if name in self._given:  # (one made on first use is cached even without a key: it proves nothing)
            return True
        try:
            api_key(settings)  # the same test the built-in adapters make: `disabled` and the like count as unset
            return True
        except NotConfigured:
            pass
        kind = settings.get("kind")
        return kind in self._kinds and kind not in ADAPTERS and not settings.get("api_key_env")

    def profile_ready(self, profile: str) -> bool:
        """Whether some candidate of `profile` can be called (Models.ready, contracts 1.6)."""
        return any(self.ready(candidate["provider"]) for candidate in self._profiles.get(profile, ()))

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

    async def _complete(self, provider: str, model: str, call):
        """`call()`, one request to `provider`, asked again after a pause while it fails transiently (a rate limit
        hands the work to another model only when it lasts)."""
        for delay in (*RETRY_DELAYS_S, None):
            await self._space(provider)
            try:
                return await call()
            except Exception as error:
                if delay is None or not transient(error):
                    raise
                pause = retry_after(error) or delay
                self._record("provider_retry", provider=provider, model=model, error=type(error).__name__,
                             delay_s=pause)
                await asyncio.sleep(pause)

    def refresh_prices(self) -> None:
        """Fetch the price list again in the background when it is a day old (never waited for: prices only label
        work, and a missing one is None)."""
        if self.prices is None or not self.prices.stale() or (self._refreshing and not self._refreshing.done()):
            return
        try:
            self._refreshing = asyncio.get_running_loop().create_task(self.prices.refresh_if_stale())
        except RuntimeError:  # (no loop running: tests calling outside one)
            self._refreshing = None

    async def generate(self, request: ModelRequest, profile: str | None = None,
                       first: Mapping[str, Any] | None = None) -> ModelResult:
        """`first`: a candidate tried before the profile's own (a model chosen with $model); the profile's
        candidates stay as the fallback."""
        name = profile or request.profile or "default"
        candidates = self._profiles.get(name)
        if not candidates:
            raise ModelError("not_configured", f"Model profile {name!r} is not configured")
        self.refresh_prices()
        if first is not None and self.known(first["provider"]):
            candidates = [dict(first), *(candidate for candidate in candidates
                                         if (candidate["provider"], candidate["model"])
                                         != (first["provider"], first["model"]))]
        failures: list[str] = []
        skipped: list[str] = []  # why each candidate before the one that answered did not (ModelResult.fallbacks)
        invalid: ModelError | None = None
        images = await asyncio.to_thread(load_images, request.images) if request.images else []
        attempts = [(candidate, bool(images) and self._sees(candidate)) for candidate in candidates]
        # Candidates that can see the images go first; the others get a note instead of the images.
        attempts.sort(key=lambda attempt: not attempt[1] and bool(images))
        unsupported = ""  # with tools: the first candidate that cannot call them, where the native attempt ends
        for candidate, with_images in attempts:
            provider, model = candidate["provider"], candidate["model"]
            if request.tools:
                messages = replay_for(provider, settle_tool_messages(request.messages))
                last_user = next((index for index in range(len(messages) - 1, -1, -1)
                                  if messages[index]["role"] == "user"), None)  # (tool results may follow it)
            else:
                messages = [dict(m) for m in request.messages]
                last_user = len(messages) - 1 if messages and messages[-1]["role"] == "user" else None
            if images and not with_images and last_user is not None:
                messages[last_user]["content"] = (str(messages[last_user].get("content") or "") + (
                    f"\n\n[{len(images)} image(s) attached, but you cannot see images; say so if they matter.]"))
            started = time.monotonic()
            try:
                if request.tools and not self.ready(provider):  # (before the adapter exists, which ready() trusts)
                    raise NotConfigured("no key")  # passed over as ever: it could not answer without tools either
                adapter = self._adapter(provider)
                extra = {"images": images} if with_images else {}
                if request.tools:
                    if not getattr(adapter, "supports_tools", False):
                        # The profile's order is its owner's preference: rather than let a later candidate answer
                        # in this one's place, the engine asks again in its JSON form, where every candidate counts.
                        unsupported = f"{provider}/{model}"
                        break
                    extra["tools"] = tuple(request.tools)
                result = await self._complete(provider, model, lambda: asyncio.wait_for(
                    adapter.complete(model, messages, request.output_schema,
                                     request.schema_name, candidate.get("effort") or request.effort,
                                     int(candidate.get("max_tokens") or request.max_tokens), request.timeout_s,
                                     **extra),
                    timeout=request.timeout_s + 5))
            except NotConfigured as error:
                failures.append(f"{provider}: not configured ({error})")
                skipped.append(f"{provider}/{model}: not configured ({error})")
                continue
            except Exception as error:
                failures.append(f"{provider}/{model}: {type(error).__name__}")
                skipped.append(f"{provider}/{model}: {type(error).__name__}: {error}"[:240])
                self._record("provider_failed", profile=name, provider=provider, model=model,
                             error=type(error).__name__, duration_s=round(time.monotonic() - started, 3))
                continue
            duration = round(time.monotonic() - started, 3)
            self._record("provider_call", profile=name, provider=provider, model=model, duration_s=duration,
                         input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                         **({"schema_mode": result.schema_mode} if result.schema_mode else {}))
            data, rejected = None, ""
            if result.truncated:
                invalid = ModelError("invalid_output", "The model output was truncated", result.text)
                failures.append(f"{provider}/{model}: truncated")
                rejected = "answer cut off at the token limit"
            elif request.tools:
                try:
                    check_calls(result, request.tools)
                except ModelError as error:
                    invalid = error
                    failures.append(f"{provider}/{model}: invalid output")
                    rejected = str(error)[:200]
            elif request.output_schema is not None:
                try:
                    data = parse_json(result.text, request.output_schema)
                except ModelError as error:
                    invalid = error
                    failures.append(f"{provider}/{model}: invalid output")
                    rejected = "blank answer (not JSON)" if not result.text.strip() else str(error)[:200]
            if rejected:
                # The call itself worked; its answer is not used, so say why (a fallback is never silent).
                skipped.append(f"{provider}/{model}: {rejected}")
                self._record("provider_rejected", profile=name, provider=provider, model=model, reason=rejected,
                             duration_s=duration, output_tokens=result.output_tokens)
                continue
            price = self.prices.price(provider, model) if self.prices is not None else None
            return ModelResult(text=result.text, data=data, provider=provider, model=model,
                               input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                               duration_s=duration, schema_mode=result.schema_mode, fallbacks=tuple(skipped),
                               cached_input_tokens=result.cached_input_tokens,
                               tool_calls=tuple(result.tool_calls),
                               replay={"provider": provider, "fields": dict(result.replay)} if result.replay else None,
                               price=price, cost=result.cost if result.cost is not None else
                               self.prices.cost(price, result.input_tokens, result.output_tokens,
                                                result.cached_input_tokens) if price else None)
        if unsupported:
            raise ModelError("tools_unsupported", f"{unsupported} cannot call tools" + (
                f"; before it: {'; '.join(failures)}" if failures else ""))
        if invalid is not None:
            raise invalid
        kind = "not_configured" if failures and all("not configured" in item for item in failures) else "unavailable"
        raise ModelError(kind, "No model candidate answered: " + "; ".join(failures))

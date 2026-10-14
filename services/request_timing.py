"""Correlated latency measurements without recording conversation contents."""
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
from time import monotonic
from uuid import uuid4


current_timing = ContextVar("request_timing", default=None)
_logger = logging.getLogger("studybot.latency")
_logger.setLevel(logging.INFO)
_logger.propagate = False
_log_lock = threading.Lock()


def _write(record):
    # Lazy initialization avoids opening a file just by importing the application.
    try:
        with _log_lock:
            if not _logger.handlers:
                handler = RotatingFileHandler("chat_latency.log", maxBytes=2_000_000, backupCount=2)
                handler.setFormatter(logging.Formatter("%(message)s"))
                _logger.addHandler(handler)
        payload = json.dumps(record, ensure_ascii=False)
        _logger.info(payload)
        print(f"[chat timing] {payload}", flush=True)
    except OSError:
        # Observability must never prevent a reply when storage is unavailable.
        pass


class RequestTiming:
    def __init__(self):
        self.request_id = uuid4().hex[:12]
        self.started = monotonic()
        self.first_delivered = False

    def record(self, stage, **values):
        _write({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request": self.request_id,
            "stage": stage,
            "since_received_s": round(monotonic() - self.started, 3),
            **values,
        })

    def delivered(self):
        if not self.first_delivered:
            self.first_delivered = True
            self.record("first_reply")


def record(stage, **values):
    timing = current_timing.get()
    if timing is not None:
        timing.record(stage, **values)


def trace_provider(actor):
    """Time each SDK attempt, including explicit retries and model fallbacks."""
    def decorate(function):
        @wraps(function)
        def traced(*args, **kwargs):
            timing = current_timing.get()
            if timing is None:
                return function(*args, **kwargs)
            started = monotonic()
            # This wrapper surrounds the individual SDK call, not an entire multi-call turn.
            model = kwargs.get("model", "")
            config = kwargs.get("config")
            effort = (kwargs.get("reasoning_effort")
                      or kwargs.get("output_config", {}).get("effort")
                      or kwargs.get("extra_body", {}).get("reasoning", {}).get("effort"))
            if config is not None:
                level = getattr(getattr(config, "thinking_config", None), "thinking_level", None)
                effort = str(getattr(level, "value", level)) if level is not None else effort
            messages = kwargs.get("messages", [])
            prompt_chars = sum(len(m.get("content", "")) for m in messages
                               if isinstance(m.get("content"), str))
            values = {"actor": actor, "model": model, "effort": effort,
                      "text_prompt_chars": prompt_chars,
                      "output_budget": kwargs.get("max_completion_tokens", kwargs.get("max_tokens",
                                         getattr(config, "max_output_tokens", None)))}
            try:
                response = function(*args, **kwargs)
                usage = getattr(response, "usage", None)
                for name in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens"):
                    value = getattr(usage, name, None)
                    if isinstance(value, int):
                        values[name] = value
                reasoning = getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None)
                if reasoning is None:
                    reasoning = getattr(getattr(usage, "output_tokens_details", None), "thinking_tokens", None)
                if isinstance(reasoning, int):
                    values["reasoning_tokens"] = reasoning
                gemini_usage = getattr(response, "usage_metadata", None)
                for source, target in (("prompt_token_count", "prompt_tokens"),
                                       ("candidates_token_count", "completion_tokens"),
                                       ("thoughts_token_count", "reasoning_tokens")):
                    value = getattr(gemini_usage, source, None)
                    if isinstance(value, int):
                        values[target] = value
                choices = getattr(response, "choices", None)
                if choices:
                    values["finish_reason"] = str(getattr(choices[0], "finish_reason", ""))
                elif getattr(response, "stop_reason", None) is not None:
                    values["finish_reason"] = str(response.stop_reason)
                else:
                    candidates = getattr(response, "candidates", None)
                    if candidates:
                        reason = getattr(candidates[0], "finish_reason", None)
                        values["finish_reason"] = str(getattr(reason, "value", reason))
                return response
            except Exception as error:
                values["error"] = type(error).__name__
                raise
            finally:
                timing.record("provider_attempt", duration_s=round(monotonic() - started, 3), **values)
        return traced
    return decorate

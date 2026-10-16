"""Wolfram|Alpha full results API (ported from providers/wolfram_query.py)."""

from __future__ import annotations

import shlex

import requests

API_URL = "https://api.wolframalpha.com/v2/query"


class WolframUnavailable(RuntimeError):
    pass


class WolframRejected(WolframUnavailable):
    pass


class WolframNotUnderstood(RuntimeError):
    pass


def command_input(text: str) -> str:
    """Accept the legacy `$wolfram --query "..."` / `-q ...` forms as well as plain text."""
    text = text.strip()
    for flag in ("--query", "-q"):
        if text == flag or text.startswith(flag + " "):
            rest = text[len(flag):].strip()
            try:
                return " ".join(shlex.split(rest))
            except ValueError:
                return rest
    return text


def query(appid: str, text: str, timeout: tuple[float, float] = (5, 30), session=None) -> list[dict]:
    """Plaintext pods as `{"title", "text"}`."""
    session = session or requests
    try:
        response = session.get(API_URL, params={"appid": appid, "input": text, "format": "plaintext",
                                                "output": "json"}, timeout=timeout)
        response.raise_for_status()
        result = response.json()["queryresult"]
    except requests.HTTPError as error:
        status = getattr(error.response, "status_code", None)
        if status in (401, 403):
            raise WolframRejected(f"HTTP {status}: the AppID was rejected") from None
        raise WolframUnavailable(f"HTTP {status or '?'}") from None
    except Exception as error:
        raise WolframUnavailable(str(error).replace(appid, "***") or type(error).__name__) from None
    error = result.get("error")
    if isinstance(error, dict):
        message = str(error.get("msg") or "unknown error").replace(appid, "***")
        raise (WolframRejected if "appid" in message.lower() else WolframUnavailable)(message)
    pods = []
    for pod in result.get("pods") or []:
        subpods = pod.get("subpods") or []
        text_value = str((subpods[0].get("plaintext") if subpods else "") or "").strip()
        pods.append({"title": str(pod.get("title") or "").strip(), "text": text_value})
    if not pods:
        raise WolframNotUnderstood("Wolfram|Alpha could not interpret that query")
    return pods

import os

from openai import OpenAI

from services.config_service import optional_secret
from services.effort import normalize_effort


BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
APP_URL = os.getenv("OPENROUTER_APP_URL", "").strip()
APP_NAME = os.getenv("OPENROUTER_APP_NAME", "DiscordStudyBot").strip()
WEB_SEARCH_MODEL = os.getenv("OPENROUTER_WEB_SEARCH_MODEL", "openrouter/auto").strip()
WEB_SEARCH_ENGINE = os.getenv("OPENROUTER_WEB_SEARCH_ENGINE", "").strip()
try:
    WEB_SEARCH_MAX_RESULTS = min(
        10,
        max(1, int(os.getenv("OPENROUTER_WEB_SEARCH_MAX_RESULTS", "5"))),
    )
except ValueError:
    WEB_SEARCH_MAX_RESULTS = 5

_api_key = optional_secret(os.getenv("OPENROUTER_API_KEY"))
_headers = {"X-OpenRouter-Title": APP_NAME} if APP_NAME else {}
if APP_URL:
    _headers["HTTP-Referer"] = APP_URL
client = (
    OpenAI(base_url=BASE_URL, api_key=_api_key, default_headers=_headers)
    if _api_key else None
)


def is_configured() -> bool:
    return client is not None


def _require_client():
    if client is None:
        raise RuntimeError("OpenRouter is not configured: OPENROUTER_API_KEY is missing")
    return client


def _require_model(model: str) -> str:
    normalized = str(model or "").strip()
    if not normalized:
        raise RuntimeError("OpenRouter model is not configured for this agent")
    return normalized


def create_completion(
    messages: list[dict],
    model: str,
    effort: str = "high",
    max_tokens: int = 2000,
    plugins: list[dict] | None = None,
):
    extra_body = {
        "reasoning": {
            "effort": normalize_effort(effort),
            "exclude": True,
        }
    }
    if plugins:
        extra_body["plugins"] = plugins
    return _require_client().chat.completions.create(
        model=_require_model(model),
        messages=messages,
        max_tokens=max_tokens,
        extra_body=extra_body,
    )


def completion_text(response, provider: str = "OpenRouter") -> str:
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if content:
        return content
    finish_reason = getattr(choice, "finish_reason", None) or "empty"
    raise RuntimeError(f"{provider} returned no answer (finish reason: {finish_reason})")


def _annotation_value(annotation, key: str):
    if isinstance(annotation, dict):
        return annotation.get(key)
    return getattr(annotation, key, None)


def _citation_sources(message) -> list[dict]:
    sources = []
    seen_urls = set()
    for annotation in getattr(message, "annotations", None) or []:
        citation = _annotation_value(annotation, "url_citation")
        if not citation:
            continue
        url = _annotation_value(citation, "url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append({
            "title": _annotation_value(citation, "title") or url,
            "url": url,
            "provider": "OpenRouter Web",
        })
    return sources


def query_web(question: str, search_query: str) -> tuple[str, list[dict]]:
    plugin = {"id": "web", "max_results": WEB_SEARCH_MAX_RESULTS}
    if WEB_SEARCH_ENGINE:
        plugin["engine"] = WEB_SEARCH_ENGINE
    response = create_completion(
        [{
            "role": "user",
            "content": (
                f"Search query: {search_query}\n\n"
                f"Answer this request using current web evidence: {question}\n"
                "Cite factual claims with markdown links to the sources."
            ),
        }],
        WEB_SEARCH_MODEL,
        effort="high",
        max_tokens=4000,
        plugins=[plugin],
    )
    message = response.choices[0].message
    return completion_text(response), _citation_sources(message)
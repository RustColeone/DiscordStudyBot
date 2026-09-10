import os
import requests
import yaml
from ddgs import DDGS

with open("config.yml", "r") as ymlfile:
    botConfig = yaml.safe_load(ymlfile)

searxng_url = (os.getenv("SEARXNG_URL") or botConfig.get("SEARXNG_URL", "")).strip().rstrip("/")


class WebSearchUnavailable(RuntimeError):
    pass


def queryWeb(query: str, max_results: int = 5) -> list[dict]:
    errors = []
    if searxng_url:
        try:
            results = _query_searxng(query, max_results)
            if results:
                return results
        except Exception as error:
            errors.append(f"SearXNG: {error}")

    try:
        results = DDGS(timeout=10).text(query, max_results=max_results)
        normalized = [
            {
                "title": result.get("title") or "Untitled result",
                "url": result.get("href") or result.get("url") or "",
                "snippet": result.get("body") or result.get("snippet") or "",
            }
            for result in results
            if result.get("href") or result.get("url")
        ]
        if normalized:
            return normalized[:max_results]
        errors.append("DuckDuckGo returned no results")
    except Exception as error:
        errors.append(f"DuckDuckGo: {error}")

    raise WebSearchUnavailable("; ".join(errors) or "No search backend returned results")


def _query_searxng(query: str, max_results: int) -> list[dict]:
    response = requests.get(
        f"{searxng_url}/search",
        params={"q": query, "format": "json"},
        headers={"Accept": "application/json"},
        timeout=(5, 15),
    )
    response.raise_for_status()
    return [
        {
            "title": result.get("title") or "Untitled result",
            "url": result.get("url") or "",
            "snippet": result.get("content") or "",
        }
        for result in response.json().get("results", [])[:max_results]
        if result.get("url")
    ]


def queryGoogle(query: str):
    """Backward-compatible tuple response for older callers."""
    results = queryWeb(query)
    return (
        [result["title"] for result in results],
        [result["url"] for result in results],
        [result["snippet"] for result in results],
    )
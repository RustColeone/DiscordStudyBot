"""Web extension: search and read public pages (ported from providers/google_query.py,
providers/web_page_query.py and the Google grounding in providers/collaborative_agent.py)."""

from __future__ import annotations

import asyncio
import os

import requests

from contracts import Capability, Contributions, Extension, Risk, ToolResult

from .pages import PageUnavailable, fetch


class SearchUnavailable(RuntimeError):
    pass


class Searcher:
    def __init__(self, config):
        self.searxng = str(config.get("searxng_url") or os.getenv("SEARXNG_URL", "")).strip().rstrip("/")
        self.grounding_model = str(config.get("grounding_model", "gemini-3.8-flash"))
        self.use_grounding = bool(config.get("grounding", True))
        self._gemini = None

    def gemini(self):
        if self._gemini is None:
            key = os.getenv("GEMINI_API_KEY", "").strip().strip("\"'")
            if not key:
                self._gemini = False
            else:
                from google import genai
                self._gemini = genai.Client(api_key=key)
        return self._gemini or None

    def grounded(self, query: str) -> tuple[str, list[dict]]:
        interaction = self.gemini().interactions.create(model=self.grounding_model, input=query,
                                                        tools=[{"type": "google_search"}], timeout=25.0)
        sources, seen = [], set()
        for step in interaction.steps or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                for annotation in getattr(block, "annotations", None) or []:
                    url = getattr(annotation, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        sources.append({"title": getattr(annotation, "title", None) or "Google Search", "url": url})
        return interaction.output_text or "", sources

    def results(self, query: str, limit: int) -> list[dict]:
        errors = []
        if self.searxng:
            try:
                response = requests.get(f"{self.searxng}/search", params={"q": query, "format": "json"},
                                        headers={"Accept": "application/json"}, timeout=(5, 15))
                response.raise_for_status()
                found = [{"title": item.get("title") or "Untitled result", "url": item["url"],
                          "snippet": item.get("content") or ""}
                         for item in response.json().get("results", []) if item.get("url")]
                if found:
                    return found[:limit]
            except Exception as error:
                errors.append(f"SearXNG: {error}")
        try:
            from ddgs import DDGS
            found = [{"title": item.get("title") or "Untitled result", "url": item.get("href") or item.get("url"),
                      "snippet": item.get("body") or item.get("snippet") or ""}
                     for item in DDGS(timeout=10).text(query, max_results=limit)
                     if item.get("href") or item.get("url")]
            if found:
                return found[:limit]
            errors.append("DuckDuckGo returned no results")
        except Exception as error:
            errors.append(f"DuckDuckGo: {error}")
        raise SearchUnavailable("; ".join(errors))

    def search(self, query: str, limit: int) -> dict:
        errors = []
        if self.use_grounding and self.gemini():
            try:
                text, sources = self.grounded(query)
                if text.strip():
                    return {"backend": "google", "answer": text.strip(), "results": sources}
                errors.append("Google grounding returned nothing")
            except Exception as error:
                errors.append(f"Google grounding: {error}")
        try:
            return {"backend": "links", "answer": "", "results": self.results(query, limit)}
        except SearchUnavailable as error:
            errors.append(str(error))
        raise SearchUnavailable("; ".join(errors))


def setup(context) -> Contributions:
    searcher = Searcher(context.config)

    async def search(arguments, tool) -> ToolResult:
        query = arguments["query"].strip()
        if not query:
            return ToolResult.fail("What should I search for?", "invalid_arguments")
        limit = max(1, min(int(arguments.get("max_results") or 5), 10))
        try:
            found = await asyncio.to_thread(searcher.search, query, limit)
        except SearchUnavailable as error:
            context.log("search_failed", error=str(error))
            return ToolResult.fail(f"Search is unavailable right now: {error}", "unavailable", retryable=True)
        lines = [found["answer"]] if found["answer"] else []
        lines += [f"- {item['title']}: {item['url']}" + (f"\n  {item['snippet']}" if item.get("snippet") else "")
                  for item in found["results"]]
        return ToolResult.ok("\n".join(lines) or "No results.", sources=tuple(item["url"] for item in found["results"]),
                             data=found)

    async def read_page(arguments, tool) -> ToolResult:
        url = arguments["url"].strip().strip("<>")
        await tool.progress(f"Reading {url}")
        try:
            page = await asyncio.to_thread(fetch, url)
        except PageUnavailable as error:
            return ToolResult.fail(f"Could not read {url}: {error}", "unavailable")
        except requests.RequestException as error:
            return ToolResult.fail(f"Could not read {url}: {error}", "unavailable", retryable=True)
        links = page["links"][:int(arguments.get("max_links") or 0)] if arguments.get("max_links") else []
        summary = f"# {page['title']}\n{page['url']}\n\n{page['text']}"
        if links:
            summary += "\n\nLinks:\n" + "\n".join(f"- {link['title']}: {link['url']}" for link in links)
        return ToolResult.ok(summary, sources=(page["url"],), data={"url": page["url"], "title": page["title"],
                                                                     "content_type": page["content_type"],
                                                                     "links": page["links"]})

    return Contributions(capabilities=[
        Capability("web.search", "Search the web for current information. Returns an answer with sources when "
                   "available, otherwise result links; read a page for detail.", search,
                   {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
                   ("query",), Risk.READ, command="search", command_argument="query", timeout_s=60),
        Capability("web.read_page", "Read the text of a public web page or PDF by URL.", read_page,
                   {"url": {"type": "string"}, "max_links": {"type": "integer", "minimum": 0, "maximum": 100}},
                   ("url",), Risk.READ, command="read", command_argument="url", timeout_s=60),
    ])


EXTENSION = Extension("web", setup, contracts="1", version="1", description="Web search and page reading")

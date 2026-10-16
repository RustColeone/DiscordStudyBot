"""Web extension: search, read public pages, find images, Google Maps and Wolfram|Alpha (ported from
providers/google_query.py, providers/web_page_query.py, providers/image_search.py,
providers/wolfram_query.py and the Google grounding in providers/collaborative_agent.py)."""

from __future__ import annotations

import asyncio
import os

import requests

from contracts import Capability, Contributions, Extension, FileRef, Risk, ToolResult

from . import wolfram
from .images import ImageUnavailable, locate_image
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
        return self.ground(query, "google_search")

    def ground(self, query: str, tool: str) -> tuple[str, list[dict]]:
        interaction = self.gemini().interactions.create(model=self.grounding_model, input=query,
                                                        tools=[{"type": tool}], timeout=25.0)
        fallback = "Google Maps" if tool == "google_maps" else "Google Search"
        sources, seen = [], set()
        for step in interaction.steps or []:
            if getattr(step, "type", None) != "model_output":
                continue
            for block in getattr(step, "content", None) or []:
                for annotation in getattr(block, "annotations", None) or []:
                    url = getattr(annotation, "url", None)
                    if url and url not in seen:
                        seen.add(url)
                        title = getattr(annotation, "title", None) or getattr(annotation, "name", None) or fallback
                        sources.append({"title": title, "url": url})
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

    async def image_search(arguments, tool) -> ToolResult:
        query = arguments["query"].strip()
        if not query:
            return ToolResult.fail("What image should I look for?", "invalid_arguments")
        await tool.progress(f"Looking for an image of {query}")
        try:
            found = await asyncio.to_thread(locate_image, query, tool.workspace,
                                            int(tool.display.max_upload_mb * 1024 * 1024))
        except ImageUnavailable as error:
            context.log("image_search_failed", error=str(error))
            return ToolResult.fail(str(error), "unavailable", retryable=True)
        return ToolResult.ok(f"Supplementary image: {found['title']}\nSource: {found['source_url']}",
                             files=(FileRef(found["path"], found["name"], found["mime"], found["size"]),),
                             sources=(found["source_url"],),
                             data={key: found[key] for key in ("title", "source_url", "image_url")})

    async def maps(arguments, tool) -> ToolResult:
        query = arguments["query"].strip()
        if not query:
            return ToolResult.fail("Which place or route should I look up?", "invalid_arguments")
        if not searcher.gemini():
            return ToolResult.fail("Google Maps is not configured (GEMINI_API_KEY is missing).", "not_configured")
        try:
            text, sources = await asyncio.to_thread(searcher.ground, query, "google_maps")
        except Exception as error:
            context.log("maps_failed", error=str(error))
            return ToolResult.fail(f"Google Maps is unavailable right now: {error}", "unavailable", retryable=True)
        if not text.strip():
            return ToolResult.fail("Google Maps returned no grounded evidence.", "no_result")
        lines = [text.strip()] + [f"- {item['title']}: {item['url']}" for item in sources]
        return ToolResult.ok("\n".join(lines), sources=tuple(item["url"] for item in sources),
                             data={"answer": text.strip(), "results": sources})

    async def wolfram_query(arguments, tool) -> ToolResult:
        query = wolfram.command_input(arguments["query"])
        if not query:
            return ToolResult.fail("What should I ask Wolfram|Alpha?", "invalid_arguments")
        appid = os.getenv("WOLFRAM_APPID", "").strip().strip("\"'")
        if not appid:
            return ToolResult.fail("Wolfram|Alpha is not configured (WOLFRAM_APPID is missing).", "not_configured")
        try:
            pods = await asyncio.to_thread(wolfram.query, appid, query)
        except wolfram.WolframNotUnderstood as error:
            return ToolResult.fail(str(error), "not_understood")
        except wolfram.WolframRejected as error:
            context.log("wolfram_failed", error=str(error))
            return ToolResult.fail(f"Wolfram|Alpha is not configured correctly: {error}", "not_configured")
        except wolfram.WolframUnavailable as error:
            context.log("wolfram_failed", error=str(error))
            return ToolResult.fail(f"Wolfram|Alpha is unavailable right now: {error}", "unavailable", retryable=True)
        summary = "\n\n".join(f"## {pod['title']}\n{pod['text']}".rstrip() for pod in pods)
        return ToolResult.ok(summary, data={"query": query, "pods": pods})

    return Contributions(capabilities=[
        Capability("web.search", "Search the web for current information. Returns an answer with sources when "
                   "available, otherwise result links; read a page for detail.", search,
                   {"query": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
                   ("query",), Risk.READ, command="search", command_argument="query", timeout_s=60),
        Capability("web.read_page", "Read the text of a public web page or PDF by URL.", read_page,
                   {"url": {"type": "string"}, "max_links": {"type": "integer", "minimum": 0, "maximum": 100}},
                   ("url",), Risk.READ, command="read", command_argument="url", timeout_s=60),
        Capability("web.image_search", "Find and send one relevant real image (diagram, object, place, artwork, "
                   "visual comparison) when seeing it would materially improve the answer. Not for decoration or "
                   "when text or a formula is enough. The image is sent to the conversation with its source.",
                   image_search, {"query": {"type": "string", "description": "Specific image-search query"}},
                   ("query",), Risk.READ, needs={"files"}, timeout_s=150),
        Capability("web.maps", "Google Maps evidence for places, nearby businesses, opening hours, directions or "
                   "location-specific recommendations. Use only for clear geographic intent; ask for a city, "
                   "address or region when the request only says 'near me'. Write the query in English.", maps,
                   {"query": {"type": "string", "description": "Concise English Google Maps query"}},
                   ("query",), Risk.READ, timeout_s=60),
        Capability("web.wolfram", "Calculate or answer a factual, mathematical or scientific query with "
                   "Wolfram|Alpha.", wolfram_query,
                   {"query": {"type": "string", "description": "Question or calculation"}},
                   ("query",), Risk.READ, command="wolfram", command_argument="query", timeout_s=60),
    ])


EXTENSION = Extension("web", setup, contracts="1", version="1",
                      description="Web search, page reading, images, maps and Wolfram|Alpha")

"""Web extension: search, read public pages, find images, Google Maps and Wolfram|Alpha (ported from
providers/google_query.py, providers/web_page_query.py, providers/image_search.py,
providers/wolfram_query.py and the Google grounding in providers/collaborative_agent.py)."""

from __future__ import annotations

import asyncio
import os

import requests

from contracts import Capability, Contributions, Extension, Setting, FileRef, Risk, ToolResult

from . import wolfram
from .images import ImageUnavailable, locate_image
from .pages import PageUnavailable, fetch


class SearchUnavailable(RuntimeError):
    pass


class Unconfigured(RuntimeError):
    """A search backend that is not set up here (no key, no URL): skipped quietly."""


# Search backends, tried in the order `search.backends` lists them (see config.example.yml). A name's own
# section may set `kind`, so one kind can be listed twice (two SearXNG servers). Adding a search API is one
# `_<kind>` method below: it returns {"answer", "results"} or raises Unconfigured.
DEFAULT_BACKENDS = ["google", "deepseek", "searxng", "duckduckgo"]
LABELS = {"google": "Google", "deepseek": "DeepSeek", "searxng": "SearXNG", "duckduckgo": "DuckDuckGo"}
DEEPSEEK_SEARCH = "https://api.deepseek.com/v1/tools/search"


class Searcher:
    def __init__(self, config):
        search = dict(config.get("search") or {})
        google = dict(search.get("google") or {})
        # Older settings still count: `grounding: false` leaves Google out, `grounding_model`, `searxng_url`.
        self.grounding_model = str(google.get("model") or config.get("grounding_model") or "gemini-3.8-flash")
        default = [name for name in DEFAULT_BACKENDS if name != "google" or bool(config.get("grounding", True))]
        # `backends` absent: the default order. Present but empty (`[]`, or nothing written after it): search is off.
        self.order = [str(name) for name in search["backends"] or []] if "backends" in search else default
        legacy_url = str(config.get("searxng_url") or os.getenv("SEARXNG_URL", "")).strip()
        self.backends = {}
        for name in self.order:
            settings = dict(search.get(name) or {})
            kind = str(settings.get("kind") or name)
            if kind == "searxng":
                settings.setdefault("url", legacy_url)
            self.backends[name] = (kind, settings)
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

    # ---- the backends --------------------------------------------------------------------------------
    def _google(self, query: str, limit: int, settings: dict) -> dict:
        """Google Search through Gemini grounding: an answer with its sources (GEMINI_API_KEY; the model is
        `search.google.model`, also used by web.maps)."""
        if not self.gemini():
            raise Unconfigured("no GEMINI_API_KEY")
        text, sources = self.grounded(query)
        return {"answer": text.strip(), "results": sources}

    def _deepseek(self, query: str, limit: int, settings: dict) -> dict:
        """DeepSeek's Search API (DEEPSEEK_API_KEY): ranked links with summaries written for models."""
        key = os.getenv(str(settings.get("api_key_env") or "DEEPSEEK_API_KEY"), "").strip().strip("\"'")
        if not key:
            raise Unconfigured("no DEEPSEEK_API_KEY")
        response = requests.post(str(settings.get("url") or DEEPSEEK_SEARCH),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                                 json={"query": query[:128], "count": max(1, min(int(settings.get("count") or limit),
                                                                                 50))}, timeout=(5, 20))
        response.raise_for_status()
        found = [{"title": item.get("title") or "Untitled result", "url": item["url"],
                  "snippet": item.get("summary") or item.get("snippet") or ""}
                 for item in response.json().get("results", []) if item.get("url")]
        return {"answer": "", "results": found[:limit]}

    def _searxng(self, query: str, limit: int, settings: dict) -> dict:
        """A SearXNG server with its JSON format on (`url`)."""
        url = str(settings.get("url") or "").strip().rstrip("/")
        if not url:
            raise Unconfigured("no url")
        response = requests.get(f"{url}/search", params={"q": query, "format": "json"},
                                headers={"Accept": "application/json"}, timeout=(5, 15))
        response.raise_for_status()
        found = [{"title": item.get("title") or "Untitled result", "url": item["url"],
                  "snippet": item.get("content") or ""}
                 for item in response.json().get("results", []) if item.get("url")]
        return {"answer": "", "results": found[:limit]}

    def _duckduckgo(self, query: str, limit: int, settings: dict) -> dict:
        """DuckDuckGo through ddgs: no key, but rate limited."""
        from ddgs import DDGS
        found = [{"title": item.get("title") or "Untitled result", "url": item.get("href") or item.get("url"),
                  "snippet": item.get("body") or item.get("snippet") or ""}
                 for item in DDGS(timeout=10).text(query, max_results=limit) if item.get("href") or item.get("url")]
        return {"answer": "", "results": found[:limit]}

    def search(self, query: str, limit: int) -> dict:
        """The first backend that finds something, with why each one before it did not (`skipped`)."""
        skipped, unset = [], []
        for name in self.order:
            kind, settings = self.backends[name]
            label = LABELS.get(name, name)
            run = getattr(self, f"_{kind}", None) if kind in LABELS else None
            if run is None:
                skipped.append(f"{label}: unknown kind {kind!r}")
                continue
            try:
                found = run(query, limit, settings)
            except Unconfigured as error:
                unset.append(f"{label}: {error}")
                continue
            except Exception as error:
                skipped.append(f"{label}: {error}"[:200])
                continue
            if found["answer"] or found["results"]:
                return {"backend": name, **found, "skipped": skipped}
            skipped.append(f"{label}: no results")
        raise SearchUnavailable("; ".join(skipped + unset) or "no search backend is set up")


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
        lines = []
        if found["skipped"]:  # a switch is never silent: say which backend answered, and why the others did not
            label = LABELS.get(found["backend"], found["backend"])
            lines.append(f"(Searched with {label}; {'; '.join(found['skipped'])})")
        lines += [found["answer"]] if found["answer"] else []
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

    # With no search backends listed, search is off: web.search is not offered (rather than always failing).
    searching = [Capability("web.search", "Search the web for current information. Returns an answer with sources "
                            "when available, otherwise result links; read a page for detail.", search,
                            {"query": {"type": "string"},
                             "max_results": {"type": "integer", "minimum": 1, "maximum": 10}},
                            ("query",), Risk.READ, command="search", command_argument="query", timeout_s=60)]
    return Contributions(capabilities=[
        *(searching if searcher.order else []),
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
                      description="Web search, page reading, images, maps and Wolfram|Alpha", settings=(
    Setting("wolfram_appid", "Wolfram|Alpha AppID for $wolfram (developer.wolframalpha.com)", "secret",
            env="WOLFRAM_APPID"),
    Setting("searxng_url", "A SearXNG server for $search, e.g. http://localhost:8080", "text", ""),
    Setting("grounding", "Search with Google through Gemini first", "boolean", True),
))

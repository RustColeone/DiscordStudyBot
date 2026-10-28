"""Model prices (core/providers/prices.py, contracts 1.12): the list against a stand-in for Artificial Analysis's free
models list, and every answer priced by the core."""

import os
import tempfile
import unittest
from pathlib import Path

from core.providers.prices import PriceBook


def aa_model(slug, creator, price_in, price_out, name=None):
    return {"id": slug, "name": name or slug, "slug": slug, "release_date": "2026-09-01",
            "model_creator": {"id": creator.lower(), "name": creator},
            "pricing": {"price_1m_input_tokens": price_in, "price_1m_output_tokens": price_out,
                        "price_1m_cache_hit_tokens": None, "price_1m_cache_write_tokens": None}}


class PriceTests(unittest.IsolatedAsyncioTestCase):
    """The price list against a stand-in for Artificial Analysis's free models list."""

    PAGES = [[aa_model("deepseek-v4-flash", "DeepSeek", 0.1, 0.4), aa_model("deepseek-v4-pro", "DeepSeek", 0.5, 2.0),
              aa_model("claude-5-5-sonnet", "Anthropic", 3, 15)],
             [aa_model("gemini-3-8-flash", "Google", 0.2, 0.8), aa_model("gemini-3-6-flash", "Google", 0.2, 0.6),
              aa_model("gemini-3-8-flash-lite", "Google", 0.05, 0.2),
              aa_model("grok-4-fast", "xAI", 0.2, 0.5), aa_model("grok-4-fast-reasoning", "xAI", 0.2, 0.5),
              aa_model("unmeasured", "Someone", None, None)]]

    async def asyncSetUp(self):
        from aiohttp import web
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.requests, self.down = [], False

        async def models(request):
            self.requests.append((request.headers.get("x-api-key"), request.query.get("page")))
            if self.down:
                return web.json_response({"error": "down"}, status=503)
            page = int(request.query.get("page", "1"))
            return web.json_response({"tier": "free", "data": self.PAGES[page - 1], "pagination": {
                "page": page, "page_size": 3, "total_pages": 2, "has_more": page < 2}})
        app = web.Application()
        app.router.add_get("/free", models)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/free"
        self.now = 1_800_000_000.0
        os.environ["TEST_AA_KEY"] = "aa-test-key"

    async def asyncTearDown(self):
        import os
        os.environ.pop("TEST_AA_KEY", None)
        await self.runner.cleanup()
        self._tmp.cleanup()

    def book(self, **settings):
        return PriceBook(self.tmp / "prices.json", {"url": self.url, "key_env": "TEST_AA_KEY", **settings},
                         clock=lambda: self.now)

    async def test_the_list_is_fetched_on_use_once_a_day_old(self):
        book = self.book()
        self.assertEqual(await book.refresh_if_stale(), "fetched")
        self.assertEqual(self.requests, [("aa-test-key", "1"), ("aa-test-key", "2")])  # every page
        self.assertEqual(len(book.cache["models"]), 8)  # an unmeasured price is left out, never taken as free
        self.now += 23 * 3600
        self.assertEqual(await book.refresh_if_stale(), "fresh")
        self.assertEqual(len(self.requests), 2)
        self.now += 2 * 3600
        self.assertEqual(await self.book().refresh_if_stale(), "fetched")  # kept on disk, so a new book knows its age
        self.assertEqual(len(self.requests), 4)

    async def test_a_failed_fetch_keeps_the_last_list_and_waits_an_hour(self):
        book = self.book()
        await book.refresh_if_stale()
        self.now += 25 * 3600
        self.down = True
        self.assertEqual(await book.refresh_if_stale(), "failed")
        self.assertEqual(book.price("deepseek", "deepseek-v4-pro")["input"], 0.5)  # the old list still prices
        self.assertEqual(await book.refresh_if_stale(), "waiting")
        self.now += 3601
        self.down = False
        self.assertEqual(await book.refresh_if_stale(), "fetched")
        self.assertIsNone(book.cache["error"])

    async def test_without_a_key_nothing_is_asked(self):
        import os
        os.environ.pop("TEST_AA_KEY")
        self.assertEqual(await self.book().refresh_if_stale(), "no_key")
        self.assertEqual(self.requests, [])

    async def test_models_are_matched_by_name_and_never_guessed(self):
        book = self.book(aliases={"gemini/gemini-fast": "gemini-3-8-flash-lite"},
                         models={"claude/claude-opus-5-5": {"input": 9, "output": 45}})
        await book.refresh_if_stale()
        self.assertEqual(book.price("deepseek", "deepseek-v4-pro")["slug"], "deepseek-v4-pro")       # exact
        self.assertEqual(book.price("claude", "claude-sonnet-5-5")["slug"], "claude-5-5-sonnet")     # words reordered
        self.assertEqual(book.price("deepseek", "deepseek-flash")["slug"], "deepseek-v4-flash")      # the one closest
        self.assertEqual(book.price("gemini", "gemini-3.8-flash")["slug"], "gemini-3-8-flash")       # dots are dashes
        self.assertEqual(book.price("grok", "grok-4-fast")["slug"], "grok-4-fast")
        self.assertIsNone(book.price("gemini", "gemini-flash"))      # 3.8 and 3.6 equally close: no guess
        self.assertEqual(book.price("gemini", "gemini-fast")["slug"], "gemini-3-8-flash-lite")       # an alias
        self.assertEqual(book.price("claude", "claude-opus-5-5")["source"], "config")                # yours win
        self.assertIsNone(book.price("openrouter", "deepseek-v4-pro-x"))
        self.assertEqual(book.cost(book.price("deepseek", "deepseek-v4-pro"), 1_000_000, 500_000), 1.5)
        status = book.status([{"seat": "gemini", "provider": "gemini", "model": "gemini-flash"},
                              {"seat": "grok", "provider": "grok", "model": "grok-4-fast"}])
        self.assertEqual((status["attribution"]["name"], status["count"]), ("Artificial Analysis", 8))
        self.assertEqual([(row["seat"], bool(row["price"]), row["candidates"][:1]) for row in status["models"]],
                         [("gemini", False, ["gemini-3-8-flash"]), ("grok", True, [])])
        self.assertEqual([entry["slug"] for _, entry in book.candidates("gemini", "gemini-flash", 2)],
                         ["gemini-3-8-flash", "gemini-3-6-flash"])  # shown on the page, to pick an alias from

    def test_a_cache_hit_costs_its_own_price_when_one_is_listed(self):
        book = self.book(models={"deepseek": {"input": 0.28, "output": 0.42, "cache_hit": 0.028}, "openai": {"input": 1.0, "output": 4.0}})
        deepseek, openai = book.price("deepseek", "deepseek-flash"), book.price("openai", "gpt-6-luna")
        self.assertEqual(book.cost(deepseek, 1_000_000, 0, 900_000), round(0.1 * 0.28 + 0.9 * 0.028, 6))
        self.assertEqual(book.cost(openai, 1_000_000, 1_000_000, 900_000), 5.0)  # no cache price: the input price
        self.assertIsNone(book.cost(None, 10, 10))


class PricedAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_answer_carries_its_price_and_cost(self):
        from contracts import AdapterResult, ModelRequest
        from core.providers.registry import Providers

        class Fake:
            async def complete(self, model, messages, *args, **kwargs):
                return AdapterResult("hi", 2_000_000, 1_000_000, cached_input_tokens=1_000_000)

        with tempfile.TemporaryDirectory() as folder:
            providers = Providers({"fake": {}}, {"p": [{"provider": "fake", "model": "m1"}]}, adapters={"fake": Fake()})
            providers.prices = PriceBook(Path(folder) / "prices.json", {"models": {"fake": {"input": 1.0, "output": 2.0,
                                                                                          "cache_hit": 0.1}}})
            result = await providers.generate(ModelRequest([{"role": "user", "content": "hello"}]), "p")
            self.assertEqual((result.price["source"], result.cost), ("config", round(1.0 + 0.1 + 2.0, 6)))
            providers.prices = PriceBook(Path(folder) / "none.json", {})
            unknown = await providers.generate(ModelRequest([{"role": "user", "content": "hello"}]), "p")
            self.assertEqual((unknown.price, unknown.cost), (None, None))  # unknown, never a guess


if __name__ == "__main__":
    unittest.main()

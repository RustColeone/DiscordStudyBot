"""Stamina (core/manager/stamina.py, contracts 1.12): what each persona may spend a day and the account she spends from;
a run that reaches her allowance, or its own budget, waits for the owner's card instead of spending more."""

import tempfile
import time
import unittest
from pathlib import Path

from contracts import Address, Request, Sender
from tests.core_support import FakeAdapter, iface, settle, start_core

# Every fake answer is 10 tokens in and 5 out: at $1,000 a million tokens, each costs $0.015.
PRICES = {"models": {"fake": {"input": 1000.0, "output": 1000.0}}}


class StaminaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, stamina):
        self.core = await start_core(self.tmp, {"prices": PRICES, "stamina": stamina},
                                     adapter=FakeAdapter(handler=lambda messages, schema: "ok"))
        self.chat = iface(self.core, "chat")
        return self.core

    def card(self):
        cards = [reply for reply in self.chat.delivered if reply.kind == "notice" and reply.code == "approval_request"
                 and reply.data.get("capability") == "core.stamina"]
        return cards[-1] if cards else None

    def run_of(self, text):
        statuses = ("running", "waiting_input", "waiting_approval", "done", "failed", "cancelled")
        return next(run for run in self.core.manager.tank.runs(statuses, limit=50) if run.trigger["text"] == text)

    async def test_a_run_at_her_daily_allowance_waits_and_carries_on_when_you_let_her(self):
        await self.start({"allowance": 0.02, "step": 0.03})
        await self.chat.say("spend 3", sender="boss", name="Boss")
        await settle(self.core)
        run = self.run_of("spend 3")
        self.assertEqual(run.status, "waiting_approval")  # two answers ($0.03) passed her $0.02: the third waits
        self.assertEqual(run.usage["model_calls"], 2)
        self.assertAlmostEqual(run.usage["cost"], 0.03)
        card = self.card()
        self.assertIn("Pat has spent her $0.020 for today. Let her spend $0.030 more today?", card.text)
        self.assertEqual([action.label for action in card.actions], ["Let her", "Stop here"])
        port = self.chat.port
        self.assertEqual(port.stamina("p")["today"], {"spent": 0.03, "allowance": 0.02, "left": 0.0,
                                                      "day": self.core.manager.stamina.day()})
        await self.chat.say(f"$approve {card.data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 3").status, "done")
        self.assertIn("spent", self.chat.texts())
        self.assertEqual(port.stamina("p")["today"]["allowance"], 0.06)  # the $0.03 she had spent, and $0.03 more
        self.assertAlmostEqual(port.stamina("p")["today"]["spent"], 0.045)
        self.assertIsNone(port.stamina("nobody"))

    async def test_letting_her_spend_more_counts_from_what_she_has_spent(self):
        await self.start({"allowance": 0.001, "step": 0.001})  # one answer ($0.015) goes far past her day
        await self.chat.say("spend 2", sender="boss", name="Boss")
        await settle(self.core)
        await self.chat.say(f"$approve {self.card().data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 2").status, "done")  # $0.001 more from the $0.015 spent, not from $0.001
        self.assertIn("Pat has spent her $0.001 for today", self.card().text)
        await self.chat.say("spend 1", sender="boss", name="Boss")  # a later stop the same day gets its own card
        await settle(self.core)
        await self.chat.say(f"$approve {self.card().data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 1").status, "done")

    async def test_saying_no_stops_the_run_at_her_limit(self):
        await self.start({"allowance": 0.01})
        await self.chat.say("spend 2", sender="boss", name="Boss")
        await settle(self.core)
        card = self.card()
        await self.chat.say(f"$deny {card.data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        run = self.run_of("spend 2")
        self.assertEqual((run.status, run.usage["model_calls"]), ("failed", 1))
        self.assertIn("reached her daily allowance", run.error)
        self.assertNotIn("spent", self.chat.texts())

    async def test_a_session_waits_at_its_own_budget(self):
        await self.start({"allowance": 0})  # no daily limit: only the run's own budget
        await self.chat.port.submit(Request(
            id="budgeted", address=Address("chat", "room"), sender=Sender("boss", "Boss"), text="spend 3",
            received_at=time.time(), is_direct=True, display=self.chat.display, extra={"budget_usd": 0.02}))
        await settle(self.core)
        self.assertEqual(self.run_of("spend 3").status, "waiting_approval")
        card = self.card()
        self.assertIn("The session spent its budget ($0.030 of $0.020). Let it spend $0.020 more?", card.text)
        self.assertEqual(card.actions[0].label, "Allow more")
        await self.chat.say(f"$approve {card.data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 3").status, "done")
        self.assertIsNone(self.chat.port.stamina("p")["today"]["allowance"])  # no daily limit

    async def test_everyones_day_has_a_total_that_waits_like_hers(self):
        await self.start({"allowance": 0, "total": 0.02})
        await self.chat.say("spend 3", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 3").status, "waiting_approval")
        card = self.card()
        self.assertIn("Everyone together has spent today's $0.020. Let them spend", card.text)
        self.assertEqual(self.chat.port.stamina()["today"]["spent"], 0.03)  # no persona: everyone's day
        await self.chat.say(f"$approve {card.data['approval_id']}", sender="boss", name="Boss")
        await settle(self.core)
        self.assertEqual(self.run_of("spend 3").status, "done")

    async def test_stamina_shows_the_day_and_changes_the_total_an_allowance_and_a_budget(self):
        await self.start({"allowance": 0.5})
        for text in ("$stamina total 3", "$stamina p 0.8", "$stamina budget fake 20", "$stamina"):
            await self.chat.say(text, sender="boss", name="Boss")
            await settle(self.core)
        said = self.chat.texts()
        self.assertTrue(any("Everyone's day: $3.00" in text for text in said), said)
        self.assertTrue(any(text.startswith("Today (") and "for everyone" in text and "Pat: $0.000 of $0.80" in text
                            for text in said), said)
        port = self.chat.port
        self.assertEqual((port.stamina()["today"]["allowance"], port.stamina("p")["today"]["allowance"]), (3.0, 0.8))
        self.core.manager.stamina.personas = lambda: {"p": self.core.manager.personas["p"]}
        stamina = self.core.manager.stamina
        self.assertEqual(stamina.account("fake")["total"], 20.0)  # a budget entered with $stamina, from today
        await self.chat.say("$stamina total 3", sender="alice", name="Alice")  # not an owner: refused
        await settle(self.core)
        self.assertEqual(stamina.total(), 3.0)

    async def test_her_own_default_allowance_comes_after_what_you_set(self):
        self.core = await start_core(self.tmp, {"prices": PRICES, "personas": {"p": {"allowance": 0.04}}},
                                     adapter=FakeAdapter(handler=lambda messages, schema: "ok"))
        stamina = self.core.manager.stamina
        self.assertEqual(self.core.manager.personas["p"].allowance, 0.04)
        self.assertEqual(stamina.allowance("p"), 0.04)  # hers (a student's `allowance:`, an agent's in core.yml)
        stamina.settings = {"allowance": 0.5}
        self.assertEqual(stamina.allowance("p"), 0.5)  # core.yml's for everyone, over hers
        stamina.settings = {"allowance": 0.5, "personas": {"p": 0.7}}
        self.assertEqual(stamina.allowance("p"), 0.7)
        stamina.command("p 0.9")
        self.assertEqual(stamina.allowance("p"), 0.9)  # `$stamina` (and the pages), over core.yml
        stamina.settings = {}
        stamina.command("p reset")
        self.assertEqual(stamina.allowance("p"), 0.04)
        self.assertEqual(stamina.allowance("nobody"), 0.5)  # no default of her own: the core's

    async def test_spending_by_day_for_the_logbook(self):
        await self.start({"allowance": 0})
        await self.chat.say("spend 2", sender="boss", name="Boss")
        await settle(self.core)
        days = self.chat.port.spending(3)
        self.assertEqual([len(days), days[-1]["day"]], [3, self.core.manager.stamina.day()])
        self.assertAlmostEqual(days[-1]["total"], 0.03)
        self.assertEqual(days[-1]["personas"], {"p": 0.03})
        self.assertEqual(days[0]["total"], 0.0)

    def test_a_balance_is_read_from_deepseek_and_from_openrouter(self):
        from core.manager.stamina import read_balance
        self.assertEqual(read_balance("deepseek", {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "118.40"}]}), (118.4, "CNY"))
        self.assertEqual(read_balance("openrouter", {"data": {"total_credits": 25, "total_usage": 4.5}}), (20.5, "USD"))
        self.assertIsNone(read_balance("deepseek", {"balance_infos": []}))

    async def test_an_entered_budget_counts_down_and_a_reported_balance_is_shown(self):
        await self.start({"allowance": 0, "accounts": {"fake": {"budget": 1.0, "since": "2026-01-01"}}})
        await self.chat.say("spend 2", sender="boss", name="Boss")
        await settle(self.core)
        account = self.chat.port.stamina("p")["account"]
        self.assertEqual((account["kind"], account["total"], account["left"]), ("budget", 1.0, 0.97))
        stamina = self.core.manager.stamina
        stamina.accounts = {"fake": {"balance": True}}
        stamina.store.set("balance:fake", {"currency": "CNY", "left": 118.4, "available": True, "as_of": 1})
        self.assertEqual(self.chat.port.stamina("p")["account"]["left"], 118.4)


if __name__ == "__main__":
    unittest.main()

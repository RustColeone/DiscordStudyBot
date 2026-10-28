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
        self.assertEqual(port.stamina("p")["today"]["allowance"], 0.05)  # her $0.02 and the $0.03 you added
        self.assertAlmostEqual(port.stamina("p")["today"]["spent"], 0.045)
        self.assertIsNone(port.stamina("nobody"))

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

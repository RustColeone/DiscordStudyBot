"""Tests for the example drop-in plugin `plugins/scholar`: the deliberate engine and its setup."""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.core_support import ROOT, FakeAdapter, iface, make_config, settle, start_core

PLUGIN = ROOT / "plugins" / "scholar"


def plan(question="", approach=("check", "answer")):
    return {"goal": "help", "approach": list(approach), "question": question}


def step(message, status="complete", actions=()):
    return {"message": message, "status": status, "actions": [{"tool": t, "arguments": a} for t, a in actions]}


def review(verdict="send", message="", problems=()):
    return {"verdict": verdict, "problems": list(problems), "message": message}


class ScholarEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.core = None
        environment = patch.dict(os.environ, {"SCHOLAR_DISCORD_TOKEN": ""})
        environment.start()
        self.addCleanup(environment.stop)

    async def asyncTearDown(self):
        if self.core is not None:
            await self.core.stop()
        self._tmp.cleanup()

    async def start(self, responses, options=None):
        self.adapter = FakeAdapter(responses)
        extensions = [*make_config(self.tmp).raw["extensions"], str(PLUGIN)]
        self.core = await start_core(self.tmp, {
            "extensions": extensions, "profiles": {"scholar": [{"provider": "fake", "model": "deep"}]},
            "assignments": {"*": {"engine": "deliberate", "personas": ["scholar"], "options": options or {}}}},
            self.adapter)
        return iface(self.core)

    def replies(self, chat):
        return [(reply.kind, reply.text) for reply in chat.delivered if reply.kind in ("text", "progress")]

    async def test_plans_works_with_tools_and_sends_only_the_reviewed_answer(self):
        chat = await self.start([plan(), step("Checking the time.", "continue", [("basic.now", {"timezone": "UTC"})]),
                                 step("It is noon."), review("revise", "It is noon (UTC).")])
        await chat.say("what time is it?")
        await settle(self.core)
        texts = [text for kind, text in self.replies(chat) if kind == "text"]
        self.assertEqual(texts, ["It is noon (UTC)."])
        progress = [text for kind, text in self.replies(chat) if kind == "progress"]
        self.assertIn("Plan: check → answer", progress)
        self.assertIn("Checking the time.", progress)
        self.assertEqual([call["effort"] for call in self.adapter.calls], ["high"] * 4)
        self.assertIn("[tool basic.now → success]", "\n".join(m["content"] for m in self.adapter.calls[2]["messages"]))

    async def test_review_can_ask_for_more_work_once(self):
        chat = await self.start([plan(), step("Probably noon."), review("more_work", problems=["not verified"]),
                                 step("Checking.", "continue", [("basic.now", {})]), step("Noon, verified."),
                                 review("more_work", problems=["still unsure"])])
        await chat.say("time?")
        await settle(self.core)
        self.assertEqual([text for kind, text in self.replies(chat) if kind == "text"], ["Noon, verified."])
        self.assertIn("[review] Not ready to send: not verified", self.adapter.calls[3]["messages"][-1]["content"])

    async def test_a_needed_question_waits_and_the_answer_continues_the_run(self):
        chat = await self.start([plan(question="Which exam board?"), step("For AQA: …"), review()])
        await chat.say("help me revise")
        await settle(self.core)
        self.assertEqual(self.core.tank.runs(("waiting_input",))[0].engine, "deliberate")
        await chat.say("AQA")
        await settle(self.core)
        self.assertEqual([text for kind, text in self.replies(chat) if kind == "text"], ["Which exam board?", "For AQA: …"])
        self.assertIn("Alice: AQA", "\n".join(m["content"] for m in self.adapter.calls[1]["messages"]))

    async def test_review_can_be_switched_off_and_the_step_limit_forces_an_answer(self):
        chat = await self.start([plan(), step("a", "continue", [("basic.now", {})]),
                                 step("b", "continue", [("basic.now", {"timezone": "UTC"})]), step("Best answer so far.")],
                                {"review": False, "max_steps": 2})
        await chat.say("dig deep")
        await settle(self.core)
        self.assertEqual([text for kind, text in self.replies(chat) if kind == "text"], ["Best answer so far."])
        self.assertIn("used all your steps", self.adapter.calls[-1]["messages"][-1]["content"])


class ScholarSetupTests(unittest.TestCase):
    def setup_with(self, config, environment):
        from core.loader import import_extension
        extension, _ = import_extension(PLUGIN)
        context = SimpleNamespace(config=config, data_dir=Path(tempfile.mkdtemp()), log=lambda *a, **k: None)
        with patch.dict(os.environ, environment):
            return extension.setup(context)

    def test_without_a_token_only_the_engine_persona_and_profile_load(self):
        contributions = self.setup_with({}, {"SCHOLAR_DISCORD_TOKEN": ""})
        self.assertEqual(([e.spec.name for e in contributions.engines], [p.name for p in contributions.personas],
                          list(contributions.interfaces)), (["deliberate"], ["scholar"], []))
        self.assertEqual(contributions.profiles["scholar"][0]["model"], "claude-opus-5-5")

    def test_with_a_token_the_bot_and_its_defaults_load(self):
        contributions = self.setup_with({"interface": "lab", "assignment": {"deny": ["system.*"]},
                                         "models": [{"provider": "openai", "model": "gpt-6.1-sol"}]},
                                        {"SCHOLAR_DISCORD_TOKEN": "secret"})
        self.assertEqual([interface.name for interface in contributions.interfaces], ["lab"])
        self.assertEqual(contributions.assignments["lab:*"]["engine"], "deliberate")
        self.assertEqual(contributions.assignments["lab:*"]["deny"], ["system.*"])
        self.assertTrue(contributions.admission["lab:*"]["require_mention_in_groups"])
        self.assertEqual(contributions.profiles["scholar"], [{"provider": "openai", "model": "gpt-6.1-sol"}])

    def test_long_text_splits_within_discords_limit(self):
        from core.loader import import_extension
        _, module = import_extension(PLUGIN)
        bot = __import__(module.__name__.rsplit(".", 1)[0] + ".bot", fromlist=["split_text"])
        for text in ("a" * 2000 + " " + "b" * 50, "x" * 1999 + "\n\n" + "y" * 10, "word " * 900):
            self.assertTrue(all(len(chunk) <= 2000 for chunk in bot.split_text(text)))


if __name__ == "__main__":
    unittest.main()

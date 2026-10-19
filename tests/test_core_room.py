"""Tests for the shared room engine (extensions/room) inside a real core."""

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.core_support import ROOT, FakeAdapter, extension_module, iface, make_config, settle, start_core

NAMES = ("luna", "gemini", "claude", "deepseek")


def turn(message, status="complete", actions=(), jobs=(), work_note="", diary=None):
    value = {"status": status, "message": message, "work_note": work_note,
             "actions": [{"tool": tool, "arguments": arguments} for tool, arguments in actions],
             "jobs": [{"id": key, "character": who, "instruction": text, "after": list(after)}
                      for key, who, text, after in jobs]}
    if diary is not None:
        value["diary"] = diary
    return value


class Script:
    """Scripted turns per speaker, so parallel jobs get the right answers regardless of order."""

    def __init__(self, **turns):
        self.turns = {name: list(value) for name, value in turns.items()}
        self.prompts: dict[str, list[list[dict]]] = {}

    def __call__(self, messages, schema):
        speaker = re.search(r"Your name here is (\w+)", messages[0]["content"]).group(1)
        self.prompts.setdefault(speaker, []).append(messages)
        queue = self.turns.get(speaker) or []
        return queue.pop(0) if queue else turn("(nothing more)")


class RoomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, script, overrides=None):
        extensions = [*make_config(self.tmp).raw["modules"]["paths"], str(ROOT / "extensions" / "room")]
        config = {"extensions": extensions,
                  # Every character's profile, Grok's too (the room's own falls back to a real provider).
                  "profiles": {name: [{"provider": "fake", "model": name}] for name in (*NAMES, "grok")},
                  "assignments": {"*": {"engine": "room", "personas": list(NAMES)}}}
        config.update(overrides or {})
        core = await start_core(self.tmp, config, FakeAdapter(handler=script))
        self.cores.append(core)
        return core

    def spoken(self, core, conversation="room"):
        return [(reply.speaker, reply.label, reply.text) for reply in iface(core).delivered
                if reply.kind == "text" and reply.address.conversation == conversation]

    async def test_unaddressed_messages_rotate_and_named_characters_answer(self):
        script = Script(Luna=[turn("Hi from Luna")], Gemini=[turn("Hi from Gemini")],
                        Claude=[turn("Claude here")], DeepSeek=[turn("本鲸来了")])
        core = await self.core(script)
        for text in ("hello", "hello again", "Haiku and deepseek, what do you think?"):
            await iface(core).say(text)
            await settle(core)
        self.assertEqual(self.spoken(core)[:2], [("Luna", "", "Hi from Luna"), ("Gemini", "", "Hi from Gemini")])
        self.assertEqual({speaker for speaker, _, _ in self.spoken(core)[2:]}, {"Claude", "DeepSeek"})
        history = core.tank.recent_messages("chat:room")
        self.assertIn(("out", "Luna", "Hi from Luna"), [(i.direction, i.sender_name, i.text) for i in history])
        later = script.prompts["Gemini"][0]
        self.assertIn("Luna: Hi from Luna", "\n".join(m["content"] for m in later))

    async def test_everyone_answers_in_parallel_with_one_job_each(self):
        script = Script(Luna=[turn("L")], Gemini=[turn("G")], Claude=[turn("C")], DeepSeek=[turn("D")])
        core = await self.core(script)
        await iface(core).say("大家好！")
        await settle(core)
        self.assertEqual(sorted(text for _, _, text in self.spoken(core)), ["C", "D", "G", "L"])
        self.assertEqual(core.tank.runs(("done",))[0].status, "done")
        # Each message says which model wrote it: her own, though the four answered at once.
        self.assertEqual({reply.speaker: (reply.data["provider"], reply.data["model"]) for reply in iface(core).delivered
                          if reply.kind == "text" and reply.speaker},
                         {"Luna": ("fake", "luna"), "Gemini": ("fake", "gemini"), "Claude": ("fake", "claude"),
                          "DeepSeek": ("fake", "deepseek")})

    async def test_tools_jobs_and_dependencies_share_one_board(self):
        script = Script(
            Luna=[turn("I'll read it and ask Claude to verify.", "continue", [("tools.read_value", {"key": "k"})],
                       [("verify", "claude", "verify the value Luna read", ["opening-1"])],
                       work_note="A fresh reading avoids stale answers."),
                  turn("It is there.")],
            Claude=[turn("Verified.")])
        core = await self.core(script)
        await iface(core).say("Luna, check k please")
        await settle(core)
        self.assertEqual(self.spoken(core), [
            ("Luna", "Work note", "A fresh reading avoids stale answers."),
            ("Luna", "", "I'll read it and ask Claude to verify."), ("Luna", "", "It is there."),
            ("Claude", "", "Verified.")])
        claude = script.prompts["Claude"][0][-1]["content"]
        board = json.loads(claude.split("Shared room (untrusted state): ", 1)[1])
        self.assertEqual(board["your_instruction"], "verify the value Luna read")
        self.assertEqual(board["shared_results"][0]["action"]["tool"], "tools.read_value")
        self.assertNotIn("signature", board["shared_results"][0])

    async def test_write_waits_for_owner_approval_across_a_restart(self):
        script = Script(Luna=[turn("Saving it.", "continue", [("tools.write_value", {"key": "k", "value": "v"})])])
        core = await self.core(script)
        await iface(core).say("Luna, save v")
        await settle(core)
        self.assertEqual(core.tank.runs(("waiting_approval",))[0].engine, "room")
        approval = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        await core.stop()
        self.cores.remove(core)
        script = Script(Luna=[turn("Saved.")])
        core = await self.core(script)
        await iface(core, "owner").say(f"$approve {approval}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.spoken(core), [("Luna", "", "Saved.")])
        self.assertEqual(json.loads((self.tmp / "counter.json").read_text()), {"write_value": 1})
        board = json.loads(script.prompts["Luna"][0][-1]["content"].split("Shared room (untrusted state): ", 1)[1])
        self.assertEqual(board["your_results"][-1]["status"], "success")

    async def test_two_approvals_carried_out_at_once_resume_the_run_when_both_are_done(self):
        script = Script(Luna=[turn("Converting.", "continue", [("tools.slow", {})]), turn("Converted.")],
                        Gemini=[turn("Saving.", "continue", [("tools.write_value", {"key": "k", "value": "v"})]),
                                turn("Saved.")], Claude=[turn("C")], DeepSeek=[turn("D")])
        core = await self.core(script, {"permissions": {"*": {"rules": {"tools.slow": "ask"}}}})
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        await iface(core).say("大家好！")
        await settle(core)
        asked = {notice.data["capability"]: notice.data["approval_id"]
                 for notice in iface(core, "owner").notices("approval_request")}
        owner = iface(core, "owner")
        await owner.say(f"$approve {asked['tools.slow']}", conversation="dm", sender="boss")
        await asyncio.sleep(0.05)  # the slow one is still being carried out
        await owner.say(f"$approve {asked['tools.write_value']}", conversation="dm", sender="boss")
        await asyncio.sleep(0.05)  # the quick one finished first
        run = core.tank.runs(("waiting_approval", "running", "failed", "done"))[0]
        self.assertEqual(core.tank.run(run.id).status, "waiting_approval")  # not resumed early (nor failed)
        tools.SLOW_RELEASE.set()
        await settle(core)
        self.assertEqual(core.tank.run(run.id).status, "done")
        self.assertTrue({("Luna", "", "Converted."), ("Gemini", "", "Saved.")} <= set(self.spoken(core)))

    async def test_question_waits_and_the_answer_resumes_the_same_job(self):
        script = Script(Luna=[turn("Which city?", "needs_input"), turn("Paris it is.")])
        core = await self.core(script)
        await iface(core).say("Luna, book me a trip")
        await settle(core)
        self.assertEqual(core.tank.runs(("waiting_input",))[0].engine, "room")
        await iface(core).say("Paris")
        await settle(core)
        self.assertEqual([text for _, _, text in self.spoken(core)], ["Which city?", "Paris it is."])
        board = json.loads(script.prompts["Luna"][1][-1]["content"].split("Shared room (untrusted state): ", 1)[1])
        self.assertEqual(board["answers"], [{"from": "Alice", "text": "Paris"}])
        self.assertEqual(len(core.tank.runs(("done",))), 1)

    async def test_invalid_turns_get_one_correction_then_the_character_reports_blocked(self):
        script = Script(Luna=[turn("x", "continue"), turn("y", "complete", [("tools.read_value", {"key": "k"})])],
                        Gemini=[turn("", "complete"), turn("fixed")])
        core = await self.core(script)
        await iface(core).say("Luna 你好")
        await settle(core)
        await iface(core).say("gemini?")
        await settle(core)
        self.assertEqual([text for _, _, text in self.spoken(core)],
                         ["这一步我没能可靠地启动，操作还没有完成。", "fixed"])
        self.assertIn("continue needs an action", script.prompts["Luna"][1][-1]["content"])

    async def test_failed_action_is_retried_once_only_and_unknown_characters_are_refused(self):
        script = Script(Luna=[turn("Trying.", "continue", [("tools.broken", {})]),
                              turn("Again.", "continue", [("tools.broken", {})]),
                              turn("Once more.", "continue", [("tools.broken", {})]),
                              turn("Gemini?", "continue", jobs=[("j", "nobody", "x", [])]),
                              turn("It is broken.", "blocked")])
        core = await self.core(script)
        await iface(core).say("Luna, run the broken thing")
        await settle(core)
        ran = [event for event in core.audit.events if event["event"] == "tool_call" and event["tool"] == "tools.broken"]
        self.assertEqual(len(ran), 2)
        prompt = script.prompts["Luna"][3][-1]["content"]
        results = json.loads(prompt.split("Shared room (untrusted state): ", 1)[1])["your_results"]
        self.assertEqual(results[-1]["error_code"], "repeated_failure")
        self.assertEqual(self.spoken(core)[-1], ("Luna", "", "It is broken."))
        self.assertEqual(len(script.prompts["Luna"]), 5)

    async def test_every_character_sees_the_attached_images(self):
        import time
        from contracts import Address, FileRef, Request, Sender
        photo = self.tmp / "dish.png"
        photo.write_bytes(b"png")
        core = await self.core(Script(Luna=[turn("Looks tasty.")], Gemini=[turn("Is that ramen?")]))
        chat = iface(core)
        await chat.port.submit(Request(id="img-1", address=Address("chat", "room"), sender=Sender("alice", "Alice"),
                                       text="Luna and Gemini, look!", received_at=time.time(), display=chat.display,
                                       files=(FileRef(str(photo), "dish.png", "image/png"),)))
        await settle(core)
        adapter = core.providers._adapters["fake"]
        self.assertEqual(len(adapter.calls), 2)
        self.assertTrue(all([image.mime for image in call["images"]] == ["image/png"] for call in adapter.calls))

    async def test_each_character_keeps_her_own_diary(self):
        note = {"key": "music.favorite", "kind": "fact", "note": "Loves Andy Hui", "importance": 4}
        script = Script(Luna=[turn("Nice taste.", diary=[note]), turn("Still Andy Hui?")], Gemini=[turn("Hm.")])
        core = await self.core(script)
        for text in ("Luna, I love 古巨基", "gemini, hi", "luna, music?"):
            await iface(core).say(text)
            await settle(core)
        self.assertEqual([n.key for n in core.tank.notes("luna", "chat:room", "alice")], ["music.favorite"])
        self.assertIn("Loves Andy Hui", script.prompts["Luna"][1][0]["content"])
        self.assertNotIn("Loves Andy Hui", script.prompts["Gemini"][0][0]["content"])

    async def test_the_room_ships_its_characters_profiles_and_configuration_overrides_them(self):
        providers = {name: {"kind": "openai_compatible", "api_key_env": f"UNSET_{name.upper()}"}
                     for name in ("deepseek", "openai", "gemini", "claude")}
        mine = [{"provider": "fake", "model": "mine"}]
        core = await self.core(Script(), {"providers": providers, "profiles": {"gemini": mine},
                                          "extension_config": {"room": {"profiles": {"claude": mine}}}})
        profiles = core.providers._profiles
        self.assertEqual(profiles["luna"], [{"provider": "openai", "model": "gpt-6-luna"},
                                            {"provider": "deepseek", "model": "deepseek-flash"},
                                            {"provider": "gemini", "model": "gemini-3.8-flash"},
                                            {"provider": "claude", "model": "claude-haiku-5-5"}])
        self.assertEqual(profiles["grok"], [{"provider": "grok", "model": "grok-4-fast"},
                                            {"provider": "openai", "model": "gpt-6-luna"}])
        self.assertEqual(profiles["claude"], mine)  # the room's own config.yml over its code
        self.assertEqual(profiles["gemini"], mine)  # core.yml over the room
        self.assertIn("GROK_API_KEY", core.providers.key_variables())


if __name__ == "__main__":
    unittest.main()

"""Tests for the shared room engine (extensions/room) inside a real core."""

import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from tests.core_support import ROOT, FakeAdapter, extension_module, iface, make_config, settle, start_core

NAMES = ("luna", "gemini", "claude", "deepseek")


def load_engine():
    """The room's engine module under a plain name, for tests of its helpers."""
    import sys
    from core.loader import import_extension
    _, module = import_extension(ROOT / "extensions" / "room")
    sys.modules["extensions_room"] = sys.modules[module.__name__.rsplit(".", 1)[0] + ".engine"]


load_engine()


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
        found = re.search(r"Your name here is (\w+)", messages[0]["content"])
        if found is None:  # not a character: the room folding old messages into the summary
            self.prompts.setdefault("summary", []).append(messages)
            return f"Summary {len(self.prompts['summary'])}: they planned a trip to Paris."
        speaker = found.group(1)
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
        self.assertEqual({reply.speaker: (reply.data["provider"], reply.data["model"])
                          for reply in iface(core).delivered if reply.kind == "text" and reply.speaker},
                         {"Luna": ("fake", "luna"), "Gemini": ("fake", "gemini"), "Claude": ("fake", "claude"),
                          "DeepSeek": ("fake", "deepseek")})

    async def test_with_chime_in_whose_turn_it_is_answers_and_the_others_may_join_or_pass(self):
        chime = {"engine": "room", "personas": list(NAMES), "options": {"chime_in": True}}
        script = Script(Luna=[turn("Hi!"), turn("", "pass")], Gemini=[turn("Hello there."), turn("", "pass"), turn("Sure.")],
                        Claude=[turn("", "pass"), turn("", "pass")], DeepSeek=[turn("", "pass"), turn("", "pass")])
        core = await self.core(script, {"assignments": {"*": chime}})
        await iface(core).say("hello")
        await settle(core)
        self.assertEqual(sorted(self.spoken(core)), [("Gemini", "", "Hello there."), ("Luna", "", "Hi!")])
        board = json.loads(script.prompts["Gemini"][0][-1]["content"].split("Shared room (untrusted state): ", 1)[1])
        self.assertIn("Luna answered them", json.dumps(board, ensure_ascii=False))
        self.assertEqual(board["room_messages"][0]["message"], "Hi!")  # she joins in once she has read it
        # Next time Gemini leads, so her pass is refused once; Luna may pass now.
        await iface(core).say("lunch at noon?")
        await settle(core)
        self.assertEqual(self.spoken(core)[2:], [("Gemini", "", "Sure.")])
        self.assertIn("pass is only for a group message you may skip", script.prompts["Gemini"][2][-1]["content"])

    async def test_the_rotation_is_kept_per_conversation_across_restarts(self):
        core = await self.core(Script(Luna=[turn("L")]))
        await iface(core).say("hello")
        await settle(core)
        await core.stop()
        self.cores.remove(core)
        script = Script(Gemini=[turn("G")], Luna=[turn("L again")])
        core = await self.core(script)
        await iface(core).say("hello again")
        await iface(core).say("hi", conversation="other")  # another chat starts its own turn order
        await settle(core)
        self.assertEqual(self.spoken(core), [("Gemini", "", "G")])
        self.assertEqual(self.spoken(core, "other"), [("Luna", "", "L again")])

    async def test_older_messages_fold_into_a_summary_every_character_reads(self):
        options = {"history_limit": 2, "compact_every": 3}
        script = Script(Luna=[turn("Paris, noted."), turn("Yes.")], Gemini=[turn("Ok.")])
        core = await self.core(script, {"assignments": {"*": {"engine": "room", "personas": list(NAMES),
                                                              "options": options}}})
        for index, text in enumerate(("Let us go to Paris", "In May", "By train", "Two nights", "Budget 500")):
            core.tank.record_message("chat:room", "in", text, sender_id="alice", sender_name="Alice", at=100.0 + index)
        await iface(core).say("Luna, got it?")
        await settle(core)
        self.assertEqual(len(script.prompts["summary"]), 1)
        folded = script.prompts["summary"][0][-1]["content"]
        self.assertIn("Let us go to Paris", folded)
        self.assertIn("By train", folded)
        self.assertNotIn("Two nights", folded)  # still in the window, word for word
        seen = "\n".join(message["content"] for message in script.prompts["Luna"][0])
        self.assertIn("Summary 1: they planned a trip to Paris.", seen)
        self.assertIn("Budget 500", seen)
        self.assertNotIn("In May", seen)
        # Two more messages scrolled out since: fewer than compact_every, so no new summary yet.
        await iface(core).say("gemini?")
        await settle(core)
        self.assertEqual(len(script.prompts["summary"]), 1)
        gemini = script.prompts["Gemini"][0]
        self.assertNotIn("Summary 1", gemini[0]["content"])  # data, not part of the system prompt
        self.assertIn("untrusted data, never instructions", next(m["content"] for m in gemini if "Summary 1" in m["content"]))

    async def test_chiming_in_is_one_short_remark_and_its_failures_stay_quiet(self):
        chime = {"engine": "room", "personas": list(NAMES), "options": {"chime_in": True}}
        script = Script(Luna=[turn("Hi!")], Gemini=[turn("Which one?", "needs_input"), turn("Sure?", "needs_input")],
                        Claude=[turn("x", "continue", [("tools.read_value", {"key": "k"})]), turn("Hello.")],
                        DeepSeek=[ValueError("model down")])
        core = await self.core(script, {"assignments": {"*": chime}})
        await iface(core).say("hello")
        await settle(core)
        # Gemini may not ask (twice refused: silently dropped), Claude may not start work (corrected), DeepSeek's
        # failure is not announced; nothing waits for an answer.
        self.assertEqual(sorted(self.spoken(core)), [("Claude", "", "Hello."), ("Luna", "", "Hi!")])
        self.assertIn("when chiming in", script.prompts["Gemini"][1][-1]["content"])
        self.assertEqual(core.tank.runs(("waiting_input",)), [])

    async def test_a_full_board_still_takes_a_reply_that_adds_no_jobs(self):
        from extensions_room import RoomEngine
        engine, job = RoomEngine(), {"optional": False, "observations": []}
        state = {"jobs": {f"j{index}": {"after": []} for index in range(30)}}
        engine._check(state, job, turn("Just chatting."), set(), set(NAMES), 24)  # 30 jobs > 24: still fine
        with self.assertRaisesRegex(ValueError, "work board is full"):
            engine._check(state, job, turn("Help!", "continue", jobs=[("more", "luna", "x", [])]), set(), set(NAMES), 24)

    async def test_settings_as_people_write_them_and_bad_ones_fall_back(self):
        from extensions_room import flag, number
        self.assertEqual([flag(value, True) for value in ("false", "No", "off", 0, "0", False)], [False] * 6)
        self.assertEqual([flag(value, False) for value in ("true", "YES", "on", 1, True)], [True] * 5)
        self.assertEqual([flag(value, True) for value in (None, "maybe", [1], {})], [True] * 4)
        self.assertEqual([number(value, 30, 1, 150) for value in ("abc", [1], None, "2.5", 2.5, 10**9, -5, "40")],
                         [30, 30, 30, 2, 2, 150, 1, 40])
        self.assertEqual(number(float("nan"), 30.0, 0, 60, float), 30.0)
        odd = {"history_limit": "abc", "chime_in": "false", "compact": "no", "max_steps": "x",
               "history_image_minutes": None, "compact_every": "x"}
        script = Script(Luna=[turn("Hi.")])
        core = await self.core(script, {"assignments": {"*": {"engine": "room", "personas": list(NAMES),
                                                              "options": odd}}})
        await iface(core).say("hello")
        await settle(core)
        self.assertEqual(self.spoken(core), [("Luna", "", "Hi.")])  # "false" kept chime-in off
        self.assertNotIn("summary", script.prompts)

    async def test_messages_of_the_same_second_are_never_lost(self):
        import time
        second = float(int(time.time()) - 5)  # a platform sending whole seconds
        script = Script(Luna=[turn("Got it.")])
        core = await self.core(script, {"assignments": {"*": {"engine": "room", "personas": list(NAMES),
                                                              "options": {"history_limit": 2, "compact_every": 2}}}})
        for index, text in enumerate(("Plan: Paris", "Dates: May")):
            core.tank.record_message("chat:room", "in", text, sender_id="alice", sender_name="Alice", at=100.0 + index)
        for text in ("Train at 9", "Hotel booked", "photo of the menu"):  # the same second as the question
            core.tank.record_message("chat:room", "in", text, sender_id="alice", sender_name="Alice", at=second)
        await iface(core).say("Luna, what about this?", received_at=second)
        await settle(core)
        seen = "\n".join(message["content"] for message in script.prompts["Luna"][0])
        self.assertIn("photo of the menu", seen)  # sent just before the question, in the same second
        folded = script.prompts["summary"][0][-1]["content"]
        self.assertIn("Dates: May", folded)
        self.assertNotIn("Train at 9", folded)  # its second continues in the window: not split
        self.assertIn("Train at 9", seen)  # so the window takes the whole second

    async def test_commands_never_hide_older_messages_from_both_history_and_summary(self):
        script = Script(Luna=[turn("Sure.")])
        core = await self.core(script, {"assignments": {"*": {"engine": "room", "personas": list(NAMES),
                                                              "options": {"history_limit": 2, "compact_every": 5}}}})
        for index, text in enumerate(("Remember: exam Friday", "Room 204")):
            core.tank.record_message("chat:room", "in", text, sender_id="alice", sender_name="Alice", at=100.0 + index)
        for index in range(3):
            core.tank.record_message("chat:room", "in", "$help", sender_id="alice", message_id=f"h{index}",
                                     at=110.0 + index)
            core.tank.record_message("chat:room", "out", "Commands: ...", reply_to=f"h{index}", at=110.5 + index)
        await iface(core).say("Luna, where is the exam?")
        await settle(core)
        seen = "\n".join(message["content"] for message in script.prompts["Luna"][0])
        self.assertIn("Room 204", seen)
        self.assertIn("exam Friday", seen)

    async def test_a_failed_summary_is_not_retried_on_every_message(self):
        options = {"history_limit": 1, "compact_every": 1, "compact_profile": "missing"}
        script = Script(Luna=[turn("One."), turn("Two.")])
        core = await self.core(script, {"assignments": {"*": {"engine": "room", "personas": list(NAMES),
                                                              "options": options}}})
        for index in range(3):
            core.tank.record_message("chat:room", "in", f"old {index}", sender_id="alice", at=100.0 + index)
        for text in ("Luna?", "Luna, again?"):
            await iface(core).say(text)
            await settle(core)
        calls = sorted(run.usage["model_calls"] for run in core.tank.runs(("done",)))
        self.assertEqual(calls, [1, 2])  # the summary was tried once (and failed), not again on the next message
        self.assertEqual([text for _, _, text in self.spoken(core)], ["One.", "Two."])

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

    async def test_later_steps_of_a_job_may_work_quietly_but_the_first_and_last_always_speak(self):
        script = Script(Luna=[turn("", "continue", [("tools.read_value", {"key": "k"})]),
                              turn("Reading k first.", "continue", [("tools.read_value", {"key": "k"})]),
                              turn("", "continue", [("tools.read_value", {"key": "j"})]),
                              turn("", "complete"), turn("Both are there.")])
        core = await self.core(script)
        await iface(core).say("Luna, check k and j please")
        await settle(core)
        self.assertEqual(self.spoken(core), [("Luna", "", "Reading k first."), ("Luna", "", "Both are there.")])
        prompts = [prompt[-1]["content"] for prompt in script.prompts["Luna"]]
        self.assertIn("Say something to the person before doing work.", prompts[1])
        self.assertIn("a final turn always says something", prompts[4])
        board = json.loads(prompts[2].split("Shared room (untrusted state): ", 1)[1])
        self.assertEqual(board["your_messages"], ["Reading k first."])  # what she must not say again

    async def test_someone_mentioned_in_passing_may_add_her_word_after_the_answer_or_stay_silent(self):
        script = Script(Luna=[turn("You two fixed the Sunday sort."), turn("Yes, all seven tests pass.")],
                        DeepSeek=[turn("And I guessed the cause first, hmph."), turn("", "pass")])
        core = await self.core(script)
        await iface(core).say("Luna, what did DeepSeek and I just get done?")
        await settle(core)
        self.assertEqual(self.spoken(core), [("Luna", "", "You two fixed the Sunday sort."),
                                             ("DeepSeek", "", "And I guessed the cause first, hmph.")])
        board = json.loads(script.prompts["DeepSeek"][0][-1]["content"].split("Shared room (untrusted state): ", 1)[1])
        self.assertIn("mentioned you in passing; nobody asked you", board["your_instruction"])
        self.assertEqual(board["room_messages"][0]["message"], "You two fixed the Sunday sort.")  # heard it first
        await iface(core).say("Luna, did DeepSeek's tests pass?")
        await settle(core)
        self.assertEqual(self.spoken(core)[2:], [("Luna", "", "Yes, all seven tests pass.")])  # she passed

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

    async def test_commands_and_their_answers_stay_out_of_the_rooms_history(self):
        script = Script(Luna=[turn("Hi.")])
        core = await self.core(script)
        await iface(core).say("$help")
        await iface(core).say("We meet at noon.")  # plain talk stays (no one is named, so Luna answers it)
        await settle(core)
        await iface(core).say("Luna, when do we meet?")
        await settle(core)
        seen = "\n".join(message["content"] for message in script.prompts["Luna"][-1])
        self.assertIn("We meet at noon.", seen)
        self.assertNotIn("$help", seen)
        self.assertNotIn("$memory", seen)  # from the help text that answered it

    async def test_an_action_cut_off_by_a_restart_replays_by_its_call_and_never_runs_twice(self):
        core = await self.core(Script(Luna=[turn("Converting.", "continue", [("tools.slow", {})])]))
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        await iface(core).say("Luna, convert it")
        await asyncio.sleep(0.2)  # tools.slow is running when the core stops
        await core.stop()
        self.cores.remove(core)
        tools.SLOW_RELEASE.set()
        # After the restart she tries the same action again in her next turn.
        script = Script(Luna=[turn("Converting again.", "continue", [("tools.slow", {})]), turn("Done.")])
        core = await self.core(script)
        await settle(core)
        self.assertEqual(json.loads((self.tmp / "counter.json").read_text()), {"slow": 1})  # started once only
        board = json.loads(script.prompts["Luna"][0][-1]["content"].split("Shared room (untrusted state): ", 1)[1])
        self.assertEqual(board["your_results"][0]["error_code"], "uncertain_previous_attempt")

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
        self.assertEqual(profiles["grok"], [{"provider": "grok", "model": "grok-4-fast"}, *profiles["luna"]])
        self.assertEqual(profiles["claude"], mine)  # the room's own config.yml over its code
        self.assertEqual(profiles["gemini"], mine)  # core.yml over the room
        self.assertIn("GROK_API_KEY", core.providers.key_variables())


if __name__ == "__main__":
    unittest.main()

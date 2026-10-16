import json
import tempfile
import unittest
from pathlib import Path

from tests.core_support import FakeAdapter, iface, settle, start_core


def turn(message="", status="complete", actions=()):
    return {"message": message, "status": status, "actions": [dict(tool=t, arguments=a) for t, a in actions]}


ASSISTANT = {"assignments": {"*": {"engine": "assistant", "personas": ["p"]}}}


class AssistantEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cores = []

    async def asyncTearDown(self):
        for core in self.cores:
            await core.stop()
        self._tmp.cleanup()

    async def core(self, responses, overrides=None):
        adapter = FakeAdapter(responses)
        core = await start_core(self.tmp, {**ASSISTANT, **(overrides or {})}, adapter)
        self.cores.append(core)
        return core, adapter

    def prompt(self, adapter, index):
        return "\n".join(m["content"] for m in adapter.calls[index]["messages"])

    def counts(self):
        path = self.tmp / "counter.json"
        return json.loads(path.read_text()) if path.exists() else {}

    async def test_speaks_then_uses_a_tool_then_answers_from_the_result(self):
        core, adapter = await self.core([turn("Let me check.", "continue", [("basic.now", {"timezone": "UTC"})]),
                                         turn("It is late.")])
        await iface(core).say("what time is it?")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["Let me check.", "It is late."])
        self.assertIn("[tool basic.now → success]", self.prompt(adapter, 1))
        system = adapter.calls[0]["messages"][0]["content"]
        self.assertIn("You are Pat.", system)
        self.assertIn('"name": "basic.now"', system)
        self.assertNotIn("tools.hidden", system)
        self.assertEqual(adapter.calls[0]["schema"]["required"], ["message", "status", "actions"])

    async def test_write_waits_for_approval_and_resumes_with_the_result_after_restart(self):
        core, adapter = await self.core([turn("Saving it.", "continue", [("tools.write_value", {"key": "k", "value": "v"})])])
        await iface(core).say("remember v")
        await settle(core)
        self.assertEqual(core.tank.runs(("waiting_approval",))[0].engine, "assistant")
        approval_id = iface(core, "owner").notices("approval_request")[0].data["approval_id"]
        await core.stop()
        self.cores.remove(core)
        core, adapter = await self.core([turn("Saved.")])
        await iface(core, "owner").say(f"$approve {approval_id}", conversation="dm", sender="boss")
        await settle(core)
        self.assertEqual(self.counts(), {"write_value": 1})
        self.assertEqual(iface(core).texts("room"), ["Saved."])
        prompt = self.prompt(adapter, 0)
        self.assertIn("remember v", prompt)
        self.assertIn("Waiting for owner approval", prompt)
        self.assertIn("[tool tools.write_value → success] wrote k (execution 1)", prompt)

    async def test_attached_images_are_shown_to_the_model_on_every_turn(self):
        import time
        from contracts import Address, FileRef, Request, Sender
        photo, notes = self.tmp / "cat.jpg", self.tmp / "notes.txt"
        photo.write_bytes(b"jpeg")
        notes.write_text("x")
        core, adapter = await self.core([turn("Looking.", "continue", [("basic.now", {})]), turn("A cat.")])
        chat = iface(core)
        await chat.port.submit(Request(id="img-1", address=Address("chat", "room"), sender=Sender("alice", "Alice"),
                                       text="what is this?", received_at=time.time(), display=chat.display,
                                       files=(FileRef(str(photo), "cat.jpg", "image/jpeg"), FileRef(str(notes), "notes.txt"))))
        await settle(core)
        self.assertEqual(chat.texts("room"), ["Looking.", "A cat."])
        self.assertEqual([[image.mime for image in call["images"]] for call in adapter.calls],
                         [["image/jpeg"], ["image/jpeg"]])

    async def test_an_image_sent_just_before_the_question_is_shown_with_it(self):
        import time
        from contracts import Address, FileRef, Sender
        photo, other = self.tmp / "dish.jpg", self.tmp / "bob.png"
        photo.write_bytes(b"jpeg")
        other.write_bytes(b"png")
        core, adapter = await self.core([turn("Ramen!")], {"assignments": {"*": {
            "engine": "assistant", "personas": ["p"], "options": {"history_scope": "sender"}}}})
        chat, room = iface(core), Address("chat", "room")
        now = time.time()
        await chat.port.observe(room, "[Image]", sender=Sender("alice", "Alice"), kind="image", message_id="m1",
                                at=now - 20, files=(FileRef(str(photo), "dish.jpg", "image/jpeg"),))
        await chat.port.observe(room, "[Image]", sender=Sender("bob", "Bob"), kind="image", message_id="m2",
                                at=now - 10, files=(FileRef(str(other), "bob.png", "image/png"),))
        await chat.port.observe(room, "[Image]", sender=Sender("alice", "Alice"), kind="image", message_id="m0",
                                at=now - 7200, files=(FileRef(str(other), "old.png", "image/png"),))
        await chat.say("@bot what is this?", is_direct=False, mentions_me=True, received_at=now)
        await settle(core)
        self.assertEqual([image.mime for image in adapter.calls[0]["images"]], ["image/jpeg"])
        prompt = self.prompt(adapter, 0)
        self.assertIn("Alice: [Image]\n[image dish.jpg, shown to you with this request]", prompt)
        self.assertNotIn("bob.png", prompt)
        stored = {item.message_id: item for item in core.tank.recent_messages("chat:room")}
        self.assertEqual(stored["m1"].files[0].name, "dish.jpg")

    async def test_images_from_tool_results_are_shown_on_later_turns(self):
        core, adapter = await self.core([turn("Making it.", "continue", [("tools.make_file", {"name": "chart.png"})]),
                                         turn("Here it is.")])
        await iface(core).say("make a chart")
        await settle(core)
        self.assertEqual([[image.mime for image in call["images"]] for call in adapter.calls], [[], ["image/png"]])

    async def test_owner_instructions_are_used_only_when_the_owner_speaks(self):
        core, adapter = await self.core([turn("Hi boss."), turn("Hi Alice.")], {"assignments": {"*": {
            "engine": "assistant", "personas": ["p"], "options": {"owner_instructions": "Call them captain."}}}})
        await iface(core).say("hello", sender="boss")
        await settle(core)
        await iface(core).say("hello", conversation="other")
        await settle(core)
        self.assertIn("Call them captain.", adapter.calls[0]["messages"][0]["content"])
        self.assertNotIn("Call them captain.", adapter.calls[1]["messages"][0]["content"])

    async def test_invalid_output_and_unknown_tools_get_one_correction_each(self):
        core, adapter = await self.core(["not json", turn("x", "continue", [("tools.nope", {})]), turn("Fine.")])
        await iface(core).say("hello")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["Fine."])
        self.assertIn("was not valid", self.prompt(adapter, 1))
        self.assertIn("unknown tools ['tools.nope']", self.prompt(adapter, 2))

    async def test_question_waits_and_the_answer_continues_the_same_run(self):
        core, adapter = await self.core([turn("Which city?", "needs_input"), turn("Paris it is.")])
        await iface(core).say("weather?")
        await settle(core)
        await iface(core).say("Paris")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["Which city?", "Paris it is."])
        self.assertEqual(len(core.tank.runs(("done",))), 1)
        self.assertIn("Paris", self.prompt(adapter, 1))

    async def test_files_from_successful_tools_are_delivered_once(self):
        core, adapter = await self.core([turn("", "continue", [("tools.make_file", {"name": "a.txt"})]), turn("Sent.")])
        await iface(core).say("make it")
        await settle(core)
        files = [reply for reply in iface(core).delivered if reply.kind == "file"]
        self.assertEqual([reply.file.name for reply in files], ["a.txt"])
        self.assertIn("[tool files] a.txt: delivered", self.prompt(adapter, 1))

    async def test_group_replies_quote_the_trigger_and_prefix_speakers(self):
        core, adapter = await self.core([turn("Hi Alice.")])
        await iface(core).say("hello bot", conversation="group", is_direct=False, mentions_me=True,
                              request_id="native-77")
        await settle(core)
        reply = [r for r in iface(core).delivered if r.kind == "text"][0]
        self.assertEqual(reply.quote, "native-77")
        self.assertIn("Alice: hello bot", self.prompt(adapter, 0))

    async def test_sender_history_scope_and_conversation_instructions(self):
        core, adapter = await self.core([turn("Hi Alice."), turn("Hi Bob."), turn("Again, Alice.")], {
            "assignments": {"chat:group": {"engine": "assistant", "personas": ["p"], "lane": "sender",
                                           "options": {"history_scope": "sender", "instructions": "Be brief."}}}})
        group = dict(conversation="group", is_direct=False, mentions_me=True)
        await iface(core).say("alice secret plan", sender="alice", **group)
        await settle(core)
        await iface(core).say("bob question", sender="bob", name="Bob", **group)
        await settle(core)
        await iface(core).say("alice again", sender="alice", **group)
        await settle(core)
        bob_prompt, alice_prompt = self.prompt(adapter, 1), self.prompt(adapter, 2)
        self.assertNotIn("alice secret plan", bob_prompt)
        self.assertIn("alice secret plan", alice_prompt)
        self.assertIn("Hi Alice.", alice_prompt)
        self.assertNotIn("bob question", alice_prompt)
        self.assertIn("Instructions for this conversation:\nBe brief.", alice_prompt)

    async def test_work_in_progress_is_visible_and_does_not_block_new_messages(self):
        import asyncio
        from tests.core_support import extension_module
        core, adapter = await self.core([turn("Converting.", "continue", [("tools.slow", {})]),
                                         turn("It's still converting."), turn("Done converting.")])
        tools = extension_module(core, "tools")
        tools.SLOW_RELEASE = asyncio.Event()
        await iface(core).say("convert the video")
        await asyncio.sleep(0.05)
        await iface(core).say("is it done yet?")
        await asyncio.sleep(0.05)
        self.assertEqual(iface(core).texts("room"), ["Converting.", "It's still converting."])
        system = adapter.calls[1]["messages"][0]["content"]
        self.assertIn('Alice: "convert the video" (running tools.slow', system)
        tools.SLOW_RELEASE.set()
        await settle(core)
        self.assertEqual(iface(core).texts("room")[-1], "Done converting.")

    async def test_quote_option_keeps_quoting_the_original_trigger(self):
        core, adapter = await self.core([turn("Working on it.", "continue", [("basic.now", {})]), turn("Done.")], {
            "assignments": {"chat:group": {"engine": "assistant", "personas": ["p"], "options": {"quote": "trigger"}}}})
        group = dict(conversation="group", is_direct=False, mentions_me=True)
        await iface(core).say("hold on", request_id="first", **group)
        await iface(core).say("also this", request_id="second", **group)
        await settle(core)
        quotes = [r.quote for r in iface(core).delivered if r.kind == "text"]
        self.assertTrue(quotes and set(quotes) == {"first"}, quotes)

    async def test_step_limit_asks_the_model_for_a_closing_message(self):
        core, adapter = await self.core([turn("Checking.", "continue", [("basic.now", {})]), turn("Out of time.", "blocked")],
                                        {"extension_config": {"assistant": {"max_steps": 1}}})
        await iface(core).say("go")
        await settle(core)
        self.assertEqual(iface(core).texts("room"), ["Checking.", "Out of time."])
        self.assertIn("You must stop now", self.prompt(adapter, 1))

    async def test_unavailable_model_fails_the_run_with_a_notice(self):
        core, adapter = await self.core([ConnectionError("down")])
        await iface(core).say("hello")
        await settle(core)
        self.assertEqual(iface(core).notices("run_failed")[0].data["error_code"], "unavailable")
        self.assertEqual(iface(core).texts("room", "text"), [])


if __name__ == "__main__":
    unittest.main()

"""$model: a chosen model per persona and conversation, live model lists, admins only."""

import json
import tempfile
import unittest
from pathlib import Path

from core.app import Core
from tests.core_support import FakeAdapter, iface, make_config, settle


class ListingAdapter(FakeAdapter):
    """A fake provider that also lists its models (counting how often it was asked)."""

    def __init__(self, models, **options):
        super().__init__(**options)
        self.models, self.listed = list(models), 0

    async def list_models(self):
        self.listed += 1
        return list(self.models)


class ModelCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fake = ListingAdapter(["fake-model", "fake-big"], handler=lambda messages, schema: "from fake")
        self.other = ListingAdapter(["m2"], handler=lambda messages, schema: "from other")
        self.core = Core(make_config(Path(self._tmp.name)), provider_adapters={"fake": self.fake, "other": self.other},
                         tick_seconds=3600)
        await self.core.start()
        self.chat = iface(self.core)

    async def asyncTearDown(self):
        await self.core.stop()
        self._tmp.cleanup()

    async def say(self, text, sender="boss", conversation="room"):
        await self.chat.say(text, sender=sender, conversation=conversation)
        await settle(self.core)
        replies = [reply for reply in self.chat.delivered if reply.address.conversation == conversation]
        return replies[-1] if replies else None

    async def test_the_overview_shows_each_personas_model_and_the_live_lists(self):
        reply = await self.say("$model")
        self.assertIn("• Pat: fake/fake-model", reply.text)
        self.assertIn("Providers: fake (2), other (1).", reply.text)
        result = json.loads(reply.data["result"])  # for interfaces that show more than text
        self.assertEqual(result["personas"], [{"name": "p", "display_name": "Pat", "model": "fake/fake-model",
                                               "chosen": False}])
        self.assertEqual(result["models"], {"fake": ["fake-big", "fake-model"], "other": ["m2"]})
        self.assertEqual((await self.say("$model other")).text, "other: m2")
        self.assertEqual((self.fake.listed, self.other.listed), (1, 1))  # asked once, then kept for an hour

    async def test_only_the_models_the_lists_offer_can_be_chosen(self):
        self.core.providers._settings["other"] = {"models_exclude": ["m2"]}
        self.assertIn("other lists no models its settings offer", (await self.say("$model other")).text)
        self.assertIn("other/m2 is not offered here", (await self.say("$model p other/m2")).text)
        self.fake.models.remove("fake-model")  # even her own model, once its provider stops listing it:
        self.core.providers._model_lists.clear()
        self.assertIn("fake/fake-model is not offered here", (await self.say("$model p fake/fake-model")).text)
        self.assertIsNone(self.core.manager.model_choices.get("chat:room", "p"))  # `reset` is the way back

    async def test_a_provider_that_cannot_be_asked_refuses_the_choice(self):
        async def down():
            raise ConnectionError("unreachable")
        self.other.list_models = down
        self.assertIn("Could not check other's models", (await self.say("$model p other/m2")).text)
        self.assertIsNone(self.core.manager.model_choices.get("chat:room", "p"))

    async def test_a_choice_goes_first_here_only_and_the_profile_stays_the_fallback(self):
        self.assertIn("Pat answers with other/m2 here", (await self.say("$model pat other/m2")).text)
        await self.say("models 1")
        self.assertEqual([call["model"] for call in self.other.calls], ["m2"])
        await self.say("models 1", conversation="elsewhere")  # another conversation keeps Pat's own model
        self.assertEqual(len(self.other.calls), 1)
        self.other.handler = lambda messages, schema: ConnectionError("down")
        before = len(self.fake.calls)
        await self.say("models 1")
        self.assertEqual(len(self.fake.calls), before + 1)  # the chosen one failed: her own answered
        self.assertIn("• Pat: other/m2 (chosen here)", (await self.say("$model")).text)
        self.assertIn("own model here again (fake/fake-model)", (await self.say("$model p reset")).text)

    async def test_unknown_models_providers_and_personas_are_refused(self):
        self.assertIn("other/nope is not offered here", (await self.say("$model p other/nope")).text)
        self.assertIn("with a provider that has a key", (await self.say("$model p ghost/x")).text)
        self.assertIn("No persona 'zed' here", (await self.say("$model zed other/m2")).text)
        self.assertIsNone(self.core.manager.model_choices.get("chat:room", "p"))

    async def test_members_cannot_and_admins_choose_with_the_owners_approval(self):
        await self.say("$model", sender="alice")
        self.assertEqual([reply.code for reply in self.chat.notices("not_permitted")], ["not_permitted"])
        self.core.manager.admins.set("chat:alice", {"added_by": "chat:boss"})
        self.assertIn("• Pat:", (await self.say("$model", sender="alice")).text)  # looking is a read
        await self.say("$model p other/m2", sender="alice")
        self.assertEqual(len(self.chat.notices("waiting_approval")), 1)  # choosing is a write
        self.assertIsNone(self.core.manager.model_choices.get("chat:room", "p"))

    async def test_models_are_never_offered_the_command_even_in_the_owners_runs(self):
        await self.say("echo hi")
        run = self.core.tank.runs(("done",))[-1]
        tools = [schema["name"] for schema in self.core.manager.tool_schemas(run.id)]
        self.assertIn("core.list_rules", tools)  # an owner-only tool is offered here,
        self.assertNotIn("core.model", tools)  # but not this one


if __name__ == "__main__":
    unittest.main()

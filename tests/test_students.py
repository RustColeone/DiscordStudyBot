"""Tests for the students module (extensions/students): the folders, the prompts built from them, the profiles."""

import tempfile
import unittest
from pathlib import Path

from tests.core_support import ROOT


def load(name):
    from core.loader import import_extension
    return import_extension(ROOT / "extensions" / name)


class Context:
    def __init__(self, config=None):
        self.name, self.config, self.data_dir, self.logged = "test", config or {}, Path("."), []

    def log(self, event, **fields):
        self.logged.append((event, fields))


class StudentFolderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.module = load("students")[1]

    def tearDown(self):
        self._tmp.cleanup()

    def student(self, name, yml, **files):
        folder = self.root / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "student.yml").write_text(yml, encoding="utf-8")
        for file, text in files.items():
            (folder / file.replace("__", "/")).write_text(text, encoding="utf-8")
        return folder

    def test_a_prompt_joins_its_files_and_each_files_lines_with_nothing_between(self):
        (self.root / "shared").mkdir()
        (self.root / "shared" / "rules.md").write_text(" 直接说话。\n\n不写心声。\n", encoding="utf-8")
        self.student("ann", "name: ann\ndisplay_name: Ann\nmodels: [{provider: p, model: m}]\n"
                     "prompt: [prompt.md, ../shared/rules.md]\n", **{"prompt.md": "你是 Ann，\n\n一名学生。\n"})
        [ann] = self.module.read_students(self.root)
        self.assertEqual(ann["prompt"], "你是 Ann，一名学生。 直接说话。不写心声。")

    def test_a_folder_must_name_itself_list_models_and_keep_its_files_inside(self):
        self.student("ann", "name: anna\ndisplay_name: Ann\nmodels: [{provider: p, model: m}]\n", **{"prompt.md": "x"})
        with self.assertRaisesRegex(ValueError, "folder's name"):
            self.module.read_students(self.root)
        self.student("ann", "name: ann\ndisplay_name: Ann\n", **{"prompt.md": "x"})
        with self.assertRaisesRegex(ValueError, "no `models`"):
            self.module.read_students(self.root)
        self.student("ann", "name: ann\ndisplay_name: Ann\nmodels: [{provider: p, model: m}]\n"
                     "prompt: [../../outside.md]\n")
        with self.assertRaisesRegex(ValueError, "outside the students folder"):
            self.module.read_students(self.root)

    def test_profiles_put_her_own_models_first_then_the_others_first_ones_in_fallback_order(self):
        a, b, c, x = ({"provider": name, "model": name} for name in "abcx")
        students = [{"name": "ann", "models": [a], "fallback": 2}, {"name": "bea", "models": [b, x], "fallback": 1},
                    {"name": "cy", "models": [c], "strong": [x]}, {"name": "dot", "models": [x], "falls_back_to": "ann"}]
        self.assertEqual(self.module.profiles(students), {
            "ann": [a, b], "bea": [b, x, a], "cy": [c, b, a], "dot": [x, a, b],
            # her tiers (Persona.profiles): her own models only, and her strong ones
            "ann_own": [a], "bea_own": [b, x], "cy_own": [c], "cy_strong": [x], "dot_own": [x]})
        self.assertEqual(self.module.tiers(students[2]), {"own": "cy_own", "strong": "cy_strong"})
        self.assertEqual(self.module.tiers(students[0]), {"own": "ann_own"})


class ShippedStudentsTests(unittest.TestCase):
    def test_the_five_students_their_order_and_what_they_bring(self):
        extension, module = load("students")
        contributions = module.setup(Context())
        self.assertEqual([persona.name for persona in contributions.personas],
                         ["gpt", "gemini", "claude", "deepseek", "grok"])
        self.assertEqual([persona.profile for persona in contributions.personas],
                         ["gpt", "gemini", "claude", "deepseek", "grok"])
        gpt, claude = contributions.personas[0], contributions.personas[2]
        self.assertEqual((gpt.display_name, gpt.aliases, gpt.formerly), ("GPT", ("Luna",), ("luna",)))
        self.assertTrue(all(persona.description for persona in contributions.personas))  # for Chimera's prompts
        self.assertEqual((gpt.tier("strong"), contributions.profiles["gpt_strong"]),
                         ("gpt_strong", [{"provider": "openai", "model": "gpt-6.1-sol"}]))
        self.assertEqual(contributions.personas[1].tier("strong"), "gemini_own")  # no strong models: her own
        self.assertEqual(claude.aliases, ("Haiku",))
        for persona in contributions.personas:  # each has her picture in her own folder
            self.assertEqual(Path(persona.pictures["avatar"]), ROOT / "extensions" / "students" / persona.name
                             / "avatar.png")
        self.assertTrue(gpt.prompt.startswith("你是 GPT，"))
        self.assertNotIn("Luna", "".join(persona.prompt for persona in contributions.personas))
        shared = module.compose(ROOT / "extensions" / "students" / "gpt",
                                ["../shared/school.md", "../shared/public.md", "../shared/math.md",
                                 "../shared/conversation.md"])
        for persona in contributions.personas:
            self.assertTrue(persona.prompt.endswith(shared), persona.name)
            self.assertNotIn("\n", persona.prompt)
        self.assertEqual(list(contributions.providers), ["grok"])
        self.assertEqual(contributions.providers["grok"]["models_since"], "2026-06-01")  # a string, as before
        self.assertEqual([(setting.key, setting.kind, setting.env) for setting in extension.settings],
                         [("grok_api_key", "secret", ("GROK_API_KEY",))])

    def test_config_overrides_providers_key_by_key_and_profiles_by_name(self):
        _, module = load("students")
        mine = [{"provider": "fake", "model": "mine"}]
        contributions = module.setup(Context({"providers": {"grok": {"models_exclude": ["imagine", "old"]}},
                                              "profiles": {"gpt": mine}}))
        self.assertEqual(contributions.providers["grok"]["models_exclude"], ["imagine", "old"])
        self.assertEqual(contributions.providers["grok"]["base_url"], "https://api.x.ai/v1")
        self.assertEqual(contributions.profiles["gpt"], mine)
        self.assertEqual(contributions.profiles["grok"][0], {"provider": "grok", "model": "grok-4-fast"})

    def test_momochat_runs_them_and_notes_models_left_in_the_rooms_config(self):
        extension, momochat = load("momochat")
        self.assertIn("room", extension.formerly)  # the room's config.yml is read as MomoChat's
        context = Context({"providers": {"grok": {"models_exclude": ["imagine"]}}})
        contributions = momochat.setup(context)
        self.assertEqual((contributions.personas, contributions.providers, contributions.profiles), ((), {}, {}))
        self.assertEqual(context.logged, [("settings_moved", {"keys": ["providers"],
                                                              "to": "extensions/students/config.yml"})])


if __name__ == "__main__":
    unittest.main()

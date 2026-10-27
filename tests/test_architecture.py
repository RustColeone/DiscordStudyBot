"""The browsable architecture page (docs/architecture) is made from the code by tools/architecture.py, so it must match
the code: every Python file, class and function, with its source."""

import ast
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load() -> dict:
    text = (ROOT / "docs" / "architecture" / "data.js").read_text(encoding="utf-8")
    return json.loads(text.split("window.ARCH = ", 1)[1].rstrip().rstrip(";"))


class ArchitecturePageTests(unittest.TestCase):
    def test_the_page_matches_the_code(self):
        result = subprocess.run([sys.executable, "tools/architecture.py", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr + "\nRun `python tools/architecture.py` and commit docs/architecture/data.js.")

    def test_every_function_is_there_with_its_own_source(self):
        data = load()
        path = "core/prompts.py"
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        defined = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        listed = {item["id"]: item for item in data["files"][path]["symbols"]}
        self.assertLessEqual(defined, set(listed))
        render = listed["render"]
        lines = data["files"][path]["text"].splitlines()[render["line"] - 1:render["end"]]
        self.assertTrue(lines[0].startswith("def render("))  # the pane shows exactly this function

    def test_the_top_map_points_at_real_places_and_reads_the_slots_from_the_modules(self):
        data = load()
        for part in data["core"]:
            self.assertTrue(part["target"] in data["files"] or part["target"] in data["folders"], part)
        self.assertIn("engines", data["modules"]["extensions/room"]["slots"])
        self.assertIn("interfaces", data["modules"]["extensions/discord"]["slots"])


if __name__ == "__main__":
    unittest.main()

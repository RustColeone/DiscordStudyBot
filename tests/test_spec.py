"""The language-neutral spec (docs/spec) stays true to the code: a port to another language can trust it."""

import json
import subprocess
import sys
import unittest

from tests.core_support import ROOT


class SpecTests(unittest.TestCase):
    def test_the_contracts_description_matches_the_code(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "describe_contracts.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "docs/spec/contracts.json is out of date: run "
                                               "`venv/bin/python tools/describe_contracts.py`")

    def test_the_lab_modules_description_matches_the_code(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "describe_module.py"), "--check"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, "docs/spec/workbench.json is out of date: run "
                                               "`venv/bin/python tools/describe_module.py`")

    def test_the_contracts_description_covers_every_export(self):
        import contracts
        described = json.loads((ROOT / "docs" / "spec" / "contracts.json").read_text(encoding="utf-8"))
        named = {*described["types"], *described["enums"], *described["interfaces"], *described["constants"]}
        missing = [name for name in contracts.__all__ if name not in named and name not in ("Handler", "AdapterFactory")]
        self.assertEqual(missing, [])  # (Handler and AdapterFactory are function types, described in prose)
        self.assertEqual(described["version"], contracts.CONTRACTS_VERSION)


if __name__ == "__main__":
    unittest.main()

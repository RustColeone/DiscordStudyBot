"""Play back the lab's conformance scenarios (tests/conformance/*.json, see tests/conformance_kit.py): the same model
answers in the same order must give the same observable trace."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.conformance_kit import HERE, Player, run
from tests.test_chimera import Club


class ConformanceTests(unittest.IsolatedAsyncioTestCase):
    maxDiff = None

    async def test_every_recorded_scenario_plays_back_the_same(self):
        files = sorted(HERE.glob("*.json"))
        self.assertTrue(files, "no scenarios: run `venv/bin/python -m tests.conformance_kit record`")
        for path in files:
            scenario = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(scenario["name"]), tempfile.TemporaryDirectory() as folder:
                clubs = []

                async def factory(adapter, **settings):
                    club = await Club(Path(folder), adapter, **settings).start()
                    clubs.append(club)
                    return club
                player = Player(scenario["answers"])
                try:
                    got, divergence = await run(factory, scenario, player)
                finally:
                    for club in clubs:
                        await club.stop()
                self.assertEqual(divergence, "", f"{path.name}: the lab asked for something else than recorded")
                self.assertEqual(player.position, len(scenario["answers"]), f"{path.name}: answers left unused")
                for key, value in scenario["expect"].items():
                    self.assertEqual(got[key], value, f"{path.name}: {key}")


if __name__ == "__main__":
    unittest.main()

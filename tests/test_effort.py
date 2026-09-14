import unittest

from services.effort import (
    deepseek_max_tokens_for_effort,
    max_tokens_for_effort,
    normalize_effort,
    planning_attempts_for_effort,
)


class EffortTests(unittest.TestCase):
    def test_max_is_a_first_class_effort(self):
        self.assertEqual(normalize_effort("max"), "max")
        self.assertEqual(max_tokens_for_effort("max"), 4000)
        self.assertEqual(deepseek_max_tokens_for_effort("max"), 384000)
        self.assertEqual(planning_attempts_for_effort("max"), 3)

    def test_unknown_effort_still_falls_back_to_high(self):
        self.assertEqual(normalize_effort("turbo"), "high")


if __name__ == "__main__":
    unittest.main()
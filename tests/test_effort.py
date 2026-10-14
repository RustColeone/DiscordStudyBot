import unittest

from services.effort import (
    deepseek_max_tokens_for_effort,
    instruction_for_response_depth,
    max_tokens_for_effort,
    max_tokens_for_response_depth,
    normalize_effort,
    normalize_response_depth,
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

    def test_response_depth_is_independent_from_reasoning_effort(self):
        self.assertEqual(normalize_response_depth("detailed"), "detailed")
        self.assertEqual(max_tokens_for_response_depth("detailed"), 8000)
        self.assertEqual(max_tokens_for_response_depth("exhaustive"), 16000)
        self.assertIn("Do not compress", instruction_for_response_depth("detailed"))

    def test_unknown_response_depth_falls_back_to_standard(self):
        self.assertEqual(normalize_response_depth("unlimited"), "standard")


if __name__ == "__main__":
    unittest.main()
from datetime import datetime, timezone
import unittest

from services.agent_context import build_agent_context


class AgentContextTests(unittest.TestCase):
    def test_formats_authoritative_local_request_time(self):
        context = build_agent_context(
            datetime(2026, 9, 14, 20, 41, tzinfo=timezone.utc),
            "America/Los_Angeles",
            "en-US",
        )

        self.assertIn("2026-09-14 20:41:00 UTC", context)
        self.assertIn("2026-09-14 13:41:00 Monday PDT-0700", context)
        self.assertIn("America/Los_Angeles", context)
        self.assertIn("en-US", context)
        self.assertIn("user's language", context)
        self.assertIn("structured fields", context)
        self.assertIn("do not replace it with the provider", context)


if __name__ == "__main__":
    unittest.main()
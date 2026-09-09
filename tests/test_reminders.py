from datetime import datetime, timedelta, timezone
import unittest

from services.reminder_service import parse_natural_reminder, resolve_timezone_name


class ReminderParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 9, 17, 30, tzinfo=timezone.utc)

    def test_parses_tomorrow_at_this_time(self):
        parsed = parse_natural_reminder(
            "remind me tomorrow at this time to submit the report",
            self.now,
            "America/Los_Angeles",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.remind_at, self.now + timedelta(days=1))
        self.assertEqual(parsed.message, "submit the report")

    def test_parses_clock_time(self):
        parsed = parse_natural_reminder(
            "$remind tomorrow at 8 PM to bring an umbrella",
            self.now,
            "America/Los_Angeles",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.remind_at.hour, 20)
        self.assertEqual(parsed.message, "bring an umbrella")

    def test_parses_compact_24_hour_time(self):
        parsed = parse_natural_reminder(
            "remind me tomorrow at 1900 to watch tv together",
            self.now,
            "America/Los_Angeles",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.remind_at.hour, 19)
        self.assertEqual(parsed.remind_at.minute, 0)

    def test_parses_chinese_reminder(self):
        parsed = parse_natural_reminder(
            "明天19点提醒我们看射雕英雄传",
            self.now,
            "America/Los_Angeles",
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.remind_at.hour, 19)
        self.assertEqual(parsed.remind_at.minute, 0)
        self.assertEqual(parsed.message, "看射雕英雄传")

    def test_rejects_missing_action(self):
        self.assertIsNone(parse_natural_reminder("remind me tomorrow", self.now, "UTC"))

    def test_uses_configured_timezone(self):
        self.assertEqual(resolve_timezone_name("Asia/Shanghai"), "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from features.feature_time import TimeFeature


REMINDER = {
    "id": 42,
    "channel_id": "channel",
    "author_id": "user",
    "author_name": "User",
    "platform": "discord",
    "message": "check the oven",
}


class ReminderDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_reminder_is_marked_only_after_successful_send(self):
        platform = SimpleNamespace(send_channel_message=AsyncMock())
        app = SimpleNamespace(platform=platform)
        now = datetime(2026, 9, 10, 2, tzinfo=timezone.utc)

        with patch("features.feature_time.db.get_due_reminders", return_value=[REMINDER]) as get_due, \
            patch("features.feature_time.db.record_reminder_attempt") as record_attempt, \
            patch("features.feature_time.db.mark_reminder_sent") as mark_sent:
            await TimeFeature()._deliver_due_reminders(app, now)

        get_due.assert_called_once_with(now)
        platform.send_channel_message.assert_awaited_once_with(
            "channel", "<@user> Reminder: check the oven"
        )
        record_attempt.assert_called_once_with(42)
        mark_sent.assert_called_once_with(42)

    async def test_failed_delivery_stays_pending_for_retry(self):
        platform = SimpleNamespace(
            send_channel_message=AsyncMock(side_effect=RuntimeError("Discord unavailable"))
        )
        app = SimpleNamespace(platform=platform)

        with patch("features.feature_time.db.get_due_reminders", return_value=[REMINDER]), \
            patch("features.feature_time.db.record_reminder_attempt"), \
            patch("features.feature_time.db.record_reminder_failure") as record_failure, \
            patch("features.feature_time.db.mark_reminder_sent") as mark_sent:
            await TimeFeature()._deliver_due_reminders(app)

        mark_sent.assert_not_called()
        record_failure.assert_called_once_with(42, "Discord unavailable")


if __name__ == "__main__":
    unittest.main()
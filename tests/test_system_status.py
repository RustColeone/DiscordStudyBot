from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from features.feature_system import SystemFeature, format_system_status
from services.system_status import SystemStatus


SAMPLE_STATUS = SystemStatus(
    timestamp=datetime(2026, 9, 8, 22, 30, tzinfo=timezone.utc),
    temperature_c=51.25,
    cpu_percent=12.5,
    cpu_count=4,
    memory_used=2 * 1024**3,
    memory_total=8 * 1024**3,
    memory_percent=25.0,
    storage_used=30 * 1024**3,
    storage_total=120 * 1024**3,
    storage_percent=25.0,
)


class SystemFeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_recognizes_system_and_status_aliases(self):
        feature = SystemFeature()

        self.assertTrue(feature.can_handle(SimpleNamespace(content="$system")))
        self.assertTrue(feature.can_handle(SimpleNamespace(content="$status")))
        self.assertFalse(feature.can_handle(SimpleNamespace(content="$systematic")))

    async def test_handle_reports_all_system_metrics(self):
        feature = SystemFeature()

        with patch("features.feature_system.collect_system_status", return_value=SAMPLE_STATUS):
            responses = await feature.handle(None, SimpleNamespace(content="$system"))

        text = responses[0].text
        self.assertIn("2026-09-08 22:30:00 UTC", text)
        self.assertIn("Temperature: `51.2 C`", text)
        self.assertIn("CPU: `12.5%` (4 cores)", text)
        self.assertIn("Memory: `2.0 GiB / 8.0 GiB` (25.0%)", text)
        self.assertIn("Storage: `30.0 GiB / 120.0 GiB` (25.0%)", text)

    def test_format_handles_missing_temperature_sensor(self):
        status = SystemStatus(**{**SAMPLE_STATUS.__dict__, "temperature_c": None})

        self.assertIn("Temperature: `Unavailable`", format_system_status(status))


if __name__ == "__main__":
    unittest.main()
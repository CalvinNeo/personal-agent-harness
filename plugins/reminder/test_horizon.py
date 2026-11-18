import importlib.util
from datetime import datetime
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo


CANDIDATE = Path(__file__).resolve().parent
PACKAGE = types.ModuleType("reminder")
PACKAGE.__path__ = [str(CANDIDATE)]
sys.modules.setdefault("reminder", PACKAGE)
spec = importlib.util.spec_from_file_location("reminder.schedule_tools", CANDIDATE / "schedule_tools.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
validate_items = module.validate_items


class ReminderHorizonTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 4, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def validate(self, event_at, *, max_delay=None):
        return validate_items(
            [{"text": "合成会议", "event_at": event_at}], 0,
            text="创建提醒", now=self.now.timestamp(), min_delay=5,
            max_delay=max_delay, max_text=500,
        )

    def test_twenty_five_day_schedule_is_allowed_without_horizon(self):
        result = self.validate("2026-11-29T19:30:00+08:00")
        self.assertEqual(len(result), 1)

    def test_year_ahead_schedule_is_allowed_without_horizon(self):
        result = self.validate("2027-11-04T12:00:00+08:00")
        self.assertEqual(len(result), 1)

    def test_past_schedule_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "已过或太近"):
            self.validate("2026-11-04T11:59:00+08:00")

    def test_explicit_custom_horizon_can_still_be_used(self):
        with self.assertRaisesRegex(ValueError, "当前配置允许"):
            self.validate("2026-11-29T19:30:00+08:00", max_delay=7 * 24 * 60 * 60)


if __name__ == "__main__":
    unittest.main()

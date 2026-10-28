import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_parser import ReminderParseError, parse_due, parse_natural


class ReminderParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 28, 10, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def assert_reminder(self, request, expected, content, *, command=False):
        result = (parse_due if command else parse_natural)(request, self.now)
        self.assertIsNotNone(result, request)
        self.assertEqual(result[0], expected, request)
        self.assertEqual(result[1], content, request)

    def timestamp(self, day=28, hour=10, minute=0, second=0):
        return self.now.replace(day=day, hour=hour, minute=minute, second=second).timestamp()

    def test_natural_relative_requests(self):
        cases = [
            ("两分钟后提醒我喝水", 120, "喝水"),
            ("请在半小时后提醒我开会", 1800, "开会"),
            ("提醒我两分钟后喝水", 120, "喝水"),
            ("过二十分钟叫我休息", 1200, "休息"),
            ("麻烦你过一个半小时提醒我休息", 5400, "休息"),
            ("请帮我一小时三十分钟后提醒我开会", 5400, "开会"),
            ("一百二十秒后提醒我喝水", 120, "喝水"),
            ("请两分钟后提醒我喝水，好吗？", 120, "喝水"),
            ("两分钟后提醒我阅读“Python”", 120, "阅读“Python”"),
            ("你能两分钟后提醒我喝水吗？", 120, "喝水"),
            ("帮我设置一个提醒，两分钟后喝水", 120, "喝水"),
        ]
        for text, seconds, body in cases:
            with self.subTest(text=text):
                self.assert_reminder(text, self.now.timestamp() + seconds, body)

    def test_natural_calendar_requests(self):
        cases = [
            ("帮我设置一个提醒，明天上午九点开会", 29, 9, 0, "开会"),
            ("明天下午三点提醒我开会", 29, 15, 0, "开会"),
            ("明天下午三点半提醒我开会", 29, 15, 30, "开会"),
            ("后天上午九点十五分提醒我出门", 30, 9, 15, "出门"),
            ("今天下午三点提醒我开会", 28, 15, 0, "开会"),
            ("今晚九点提醒我睡觉", 28, 21, 0, "睡觉"),
            ("下午三点提醒我开会", 28, 15, 0, "开会"),
            ("上午九点提醒我开会", 29, 9, 0, "开会"),
            ("提醒我明天09:30开会", 29, 9, 30, "开会"),
            ("2026-10-29 09:30提醒我开会", 29, 9, 30, "开会"),
        ]
        for text, day, hour, minute, body in cases:
            with self.subTest(text=text):
                self.assert_reminder(text, self.timestamp(day, hour, minute), body)

    def test_existing_command_grammar(self):
        cases = [
            ("2分钟后喝水", self.now.timestamp() + 120, "喝水"),
            ("半分钟后提醒我喝水", self.now.timestamp() + 30, "喝水"),
            ("2m 后 开会", self.now.timestamp() + 120, "开会"),
            ("2026-10-29 18:30 开会", self.timestamp(29, 18, 30), "开会"),
            ("2026/10/29 18:30:05 提醒我开会", self.timestamp(29, 18, 30, 5), "开会"),
            ("18:30 开会", self.timestamp(28, 18, 30), "开会"),
            ("09:30 开会", self.timestamp(29, 9, 30), "开会"),
        ]
        for text, due, body in cases:
            with self.subTest(text=text):
                self.assert_reminder(text, due, body, command=True)

    def test_conversation_is_not_scheduled(self):
        cases = [
            "今天天气怎么样", "我两分钟后去喝水", "两分钟后喝水",
            "我记得你两分钟后提醒我喝水", "他让我两分钟后提醒我喝水",
            "他说两分钟后提醒我喝水", "如果两分钟后提醒我喝水会怎样",
            "例如两分钟后提醒我喝水", "不要两分钟后提醒我喝水",
            "两分钟后不要提醒我喝水", "不用提醒我两分钟后喝水",
            "取消两分钟后提醒我喝水", "如何设置两分钟后提醒我喝水",
            "你支持两分钟后提醒我喝水这种语法吗", "提醒功能怎么使用",
            "“两分钟后提醒我喝水”", "请解释“两分钟后提醒我喝水”",
            "你能提醒我吗", "提醒我吗", "/提醒 两分钟后喝水",
            "两分钟后提醒我喝水已经设置了吗", "有没有提醒我两分钟后喝水",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(parse_natural(text, self.now), text)

    def test_explicit_requests_requiring_clarification(self):
        cases = [
            "三点提醒我开会", "明天三点提醒我开会", "明天提醒我开会",
            "一会儿提醒我喝水", "提醒我喝水", "两分钟后提醒我",
            "帮我设置一个提醒", "每天上午九点提醒我喝水",
            "每隔两分钟提醒我喝水", "两分钟后提醒我喝水，三分钟后提醒我吃药",
            "两分钟后提醒小明喝水", "提醒他两分钟后喝水",
            "两分钟后提醒我喝水，三分钟后吃药",
            "帮我设置一个提醒，明天上午九点提醒小明喝水",
            "2026-02-30 12:00 提醒我开会", "明天25:00提醒我开会",
            "晚上十二点提醒我开会", "零分钟后提醒我喝水",
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ReminderParseError, msg=text):
                    parse_natural(text, self.now)

    def test_invalid_command_returns_none(self):
        for text in ["abc", "3点开会", "2m 后", "25:00 开会", "2026-02-30 12:00 开会", "零分钟后喝水"]:
            with self.subTest(text=text):
                self.assertIsNone(parse_due(text, self.now))

    def test_explicit_past_dates_not_silently_changed(self):
        self.assert_reminder("今天上午九点提醒我开会", self.timestamp(28, 9), "开会")
        # The plugin checks its min/max horizon and rejects a past result.


if __name__ == "__main__":
    unittest.main()

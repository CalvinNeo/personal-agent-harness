"""Small, deterministic Chinese reminder parser; no model or network access.

``parse_natural`` only claims clear requests addressed to the bot.  It deliberately
leaves ordinary conversation alone, and refuses to guess ambiguous times.
All dates use the timezone of the supplied ``now`` (normally Asia/Shanghai).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta


class ReminderParseError(ValueError):
    """An explicit reminder request needs clarification instead of scheduling."""


_NUM = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+|半)"
_UNIT = r"(?:秒钟?|分钟?|小时|时|天|日|周|星期|[smhdw])"
_DURATION_PART = re.compile(rf"(?P<number>{_NUM})\s*(?:个\s*)?(?P<prehalf>半)?(?P<unit>{_UNIT})(?P<half>半)?", re.I)
_RELATIVE = re.compile(rf"^(?P<prefix>在|再过|过)?\s*(?P<duration>(?:{_NUM}\s*(?:个\s*)?半?{_UNIT}半?\s*)+)(?P<after>之?后|以后)?\s*", re.I)
_DATE = re.compile(r"^(?P<year>\d{4})[-/](?P<month>\d{1,2})[-/](?P<day>\d{1,2})\s+")
_DAY = re.compile(r"^(今天|明天|后天|今日|明日)\s*")
_PERIOD = re.compile(r"^(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|今晚)\s*")
_CLOCK = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?(?!\d)\s*")
_CN_CLOCK = re.compile(rf"^(?P<hour>{_NUM})\s*(?:点|时)(?:(?P<half>半)|(?P<minute>{_NUM})\s*分?)?\s*")
_INTENT = re.compile(r"(?:提醒|通知|叫)(?:一下|下)?我")
_SETUP = re.compile(r"^(?:设置|设|创建|建立|安排|添加|定)(?:置)?(?:一下|个|一个|一条)?(?:定时)?提醒(?:任务)?\s*[，,:：、]?\s*")
_POLITE = re.compile(r"^(?:请你|请|麻烦你|麻烦|劳烦|帮我|给我|你能不能|能不能|你可以|可以|你能否|能否|能|你|一下)\s*")
_NEGATIVE = re.compile(r"不要|不用|无需|不必|别(?:再)?(?:帮我|给我|提醒|通知|叫|设置|创建|安排)|取消|撤销|停止|不想|不需要|不希望|不(?:要)?再")
_DISCUSSION = re.compile(r"怎么|如何|为什么|是什么|什么意思|什么叫|是否支持|支持.{0,8}(?:提醒|定时)|(?:提醒|定时).{0,8}(?:功能|语法|命令|示例|例子)|(?:介绍|解释|讲解|演示).{0,8}(?:提醒|定时)")
_STATUS_QUESTION = re.compile(r"(?:设置|创建|安排|完成|触发|执行|发送|收到|取消|成功|失败)(?:了|过|好)?(?:吗|么|没|没有|了吗)[？?。]*$|(?:有没有|是否|有无).{0,12}(?:提醒|通知)")
_REPORTED = re.compile(r"(?:他说|她说|他说过|她说过|有人说|朋友说|小明说|他说的|她说的|原话|转发|引用|转述|例如|比如|假如|如果|假设|测试文本|测试语句)")
_RECURRENCE = re.compile(r"每天|每日|每晚|每早|每周|每星期|每月|每年|每隔|每\s*(?:\d+|[一二两三四五六七八九十百]+)?\s*(?:秒|分|小时|天|周|星期)|定期|循环|周期|反复|重复")
_OTHER_PERSON = re.compile(r"(?:提醒|通知|叫)(?:一下|下)?(?!我)(?:大家|所有人|全体|他|她|它|他们|她们|你们|[\u4e00-\u9fffA-Za-z@])")
_TRIM = " \t\r\n，,。；;：:、！!"
_UNITS = {"秒": 1, "秒钟": 1, "s": 1, "分": 60, "分钟": 60, "m": 60,
          "小时": 3600, "时": 3600, "h": 3600, "天": 86400, "日": 86400,
          "d": 86400, "周": 604800, "星期": 604800, "w": 604800}
_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))


def _number(value: str) -> float:
    if value == "半":
        return 0.5
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    if not any(unit in value for unit in "十百千万"):
        return float("".join(str(_DIGITS[char]) for char in value))
    total = section = digit = 0
    for char in value:
        if char in _DIGITS:
            digit = _DIGITS[char]
        elif char == "万":
            total += (section + digit) * 10000
            section = digit = 0
        else:
            section += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return float(total + section + digit)


def _time_prefix(text: str, now: datetime) -> tuple[float, str] | None:
    """Consume a time at the start, leaving the intended reminder content."""
    text = re.sub(r"^在\s*", "", text.strip(), count=1)
    match = _RELATIVE.match(text)
    if match and (match.group("after") or match.group("prefix") in {"过", "再过"}):
        seconds = sum((_number(part.group("number")) + (0.5 if part.group("half") or part.group("prehalf") else 0))
                      * _UNITS[part.group("unit").lower()]
                      for part in _DURATION_PART.finditer(match.group("duration")))
        if seconds <= 0:
            raise ReminderParseError("提醒时间必须在未来，请写明几分钟后或具体日期时间。")
        return now.timestamp() + seconds, text[match.end():]

    day_offset = None
    exact_date = None
    match = _DATE.match(text)
    if match:
        try:
            exact_date = now.replace(year=int(match["year"]), month=int(match["month"]), day=int(match["day"]))
        except ValueError as error:
            raise ReminderParseError("日期无效，请使用例如“2026-10-29 09:30”。") from error
        text = text[match.end():]
    else:
        match = _DAY.match(text)
        if match:
            day_offset = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2}[match[1]]
            text = text[match.end():]

    period = None
    match = _PERIOD.match(text)
    if match:
        period = match[1]
        if period == "今晚":
            if day_offset is not None or exact_date is not None:
                raise ReminderParseError("日期有歧义，请使用例如“今天晚上九点”。")
            day_offset, period = 0, "晚上"
        text = text[match.end():]

    match = _CLOCK.match(text)
    if match:
        hour, minute, second = int(match["hour"]), int(match["minute"]), int(match["second"] or 0)
    else:
        match = _CN_CLOCK.match(text)
        if not match:
            if day_offset is not None or exact_date is not None or period is not None:
                raise ReminderParseError("请补充具体时间，例如“明天上午九点提醒我开会”。")
            return None
        hour_value = _number(match["hour"])
        minute_value = 30 if match["half"] else _number(match["minute"] or "零")
        if not hour_value.is_integer() or not float(minute_value).is_integer():
            raise ReminderParseError("时间无效，请使用例如“下午三点半”或“15:30”。")
        hour, minute, second = int(hour_value), int(minute_value), 0
        if period is None and hour <= 12:
            raise ReminderParseError("请说明上午或下午，例如“下午三点提醒我开会”，也可以写“15:00”。")

    if period:
        if period in {"凌晨", "早上", "早晨", "上午"}:
            if not 0 <= hour <= 11:
                raise ReminderParseError("上午时间请使用 0 至 11 点；中午十二点可写“12:00”。")
        elif period == "中午":
            if hour in (1, 2):
                hour += 12
            elif hour not in (11, 12, 13, 14):
                raise ReminderParseError("中午时间有歧义，请改用明确的 24 小时时间。")
        else:
            if 1 <= hour <= 11:
                hour += 12
            elif not 12 <= hour <= 23:
                raise ReminderParseError("下午或晚上时间有歧义，请改用明确的 24 小时时间。")
            if period in {"晚上", "傍晚"} and hour == 12:
                raise ReminderParseError("晚上十二点存在日期歧义，请写明日期和“00:00”。")

    try:
        due = (exact_date or now + timedelta(days=day_offset or 0)).replace(
            hour=hour, minute=minute, second=second, microsecond=0)
    except ValueError as error:
        raise ReminderParseError("时间无效，请使用例如“09:30”或“下午三点半”。") from error
    if exact_date is None and day_offset is None and due <= now:
        due += timedelta(days=1)
    return due.timestamp(), text[match.end():]


def _content(value: str) -> str:
    value = value.strip(_TRIM)
    value = re.sub(r"^(?:提醒|通知|叫)(?:一下|下)?我\s*", "", value)
    value = re.sub(r"^(?:提醒|通知)\s*", "", value)
    value = value.strip(_TRIM)
    value = re.sub(r"(?:可以吗|好吗|行吗|好不好)[？?]?\s*$", "", value).strip(_TRIM)
    if not value or value in {"一下", "下", "我", "吗", "嘛"}:
        raise ReminderParseError("请告诉我要提醒什么，例如“两分钟后提醒我喝水”。")
    return value


def parse_due(text: str, now: datetime) -> tuple[float, str] | None:
    """Parse the command body; invalid or unsupported input returns ``None``.

    Existing ``/提醒`` inputs (``2m 后 开会``, absolute dates and ``HH:MM``)
    remain supported.  Natural-language requests are also accepted.
    """
    try:
        text = text.strip()
        natural = parse_natural(text, now)
        if natural is not None:
            return natural
        result = _time_prefix(text, now)
        if result is None:
            return None
        due, content = result
        return due, _content(content)
    except (ReminderParseError, OverflowError, ValueError):
        return None


def parse_natural(text: str, now: datetime) -> tuple[float, str] | None:
    """Return None for conversation, a reminder tuple, or a helpful parse error.

    Recognition requires an explicit self-reminder request or a setup phrase.
    Negated, quoted, hypothetical and explanatory messages are left untouched.
    Recurring reminders, ambiguous time and reminders for others are rejected.
    """
    text = text.strip()
    if not text or text.startswith(("/", "'", '"', "“", "‘", "「", "『", ">", "```")):
        return None
    if _NEGATIVE.search(text) or _DISCUSSION.search(text) or _REPORTED.search(text) or _STATUS_QUESTION.search(text):
        return None
    # A whole quoted sentence is an example, not an instruction. Quotes inside
    # the reminder's content remain valid (e.g. read the book “Python”).
    if re.search(r"[“「『\"].*(?:提醒|通知|叫).*?[”」』\"]", text):
        return None

    modal_question = bool(re.match(r"^(?:请问)?(?:你)?(?:能不能|能否|可以|能)", text))
    if modal_question and text.endswith(("吗", "吗？", "吗?")):
        text = re.sub(r"吗[？?]?$", "", text)

    for _ in range(8):
        shortened = _POLITE.sub("", text, count=1)
        if shortened == text:
            break
        text = shortened
    if modal_question and _INTENT.fullmatch(text.strip(_TRIM)):
        return None
    setup = _SETUP.match(text)
    intent = _INTENT.search(text)
    if setup is None and intent is None:
        # Clearly asks the bot to remind another person: explain the restriction.
        other = _OTHER_PERSON.search(text)
        if other and (re.match(r"^(?:在|过|再过)?(?:\d|[一二两三四五六七八九十半今明后])", text)
                      or other.start() == 0):
            raise ReminderParseError("目前只能提醒你自己，请使用“提醒我”；暂不支持替其他人创建提醒。")
        return None
    if re.search(r"(?:能|可以|会|能够).{0,8}(?:提醒|通知|叫)我[吗嘛？?]*$", text) or text in {"提醒我吗", "提醒我？", "提醒我?"}:
        return None
    if _RECURRENCE.search(text):
        raise ReminderParseError("目前只支持一次性提醒，暂不支持每天、每周或循环提醒；请提供一次具体时间。")
    if len(list(_INTENT.finditer(text))) > 1 or re.search(r"[，,；;](?:然后|再|和)?\s*(?:明天|后天|今天|下午|上午|晚上|\d{1,2}:|(?:\d+|[一二两三四五六七八九十半]+)(?:分钟|小时|秒)后)", text) and not setup:
        raise ReminderParseError("请一次创建一条提醒；多个提醒请分开发送。")

    if setup:
        body = text[setup.end():]
    elif intent and intent.start() == 0:
        body = text[intent.end():].lstrip(_TRIM)
    else:
        # A time must start the request, preventing conversation such as
        # “我记得你两分钟后提醒我喝水” from becoming an instruction.
        body = text
    result = _time_prefix(body, now)
    if result is None:
        if setup or (intent and intent.start() == 0) or re.match(r"^(?:在|过|再过|今天|明天|后天|上午|下午|晚上|凌晨|早上|明早|今晚|\d|[一二两三四五六七八九十半])", body):
            raise ReminderParseError("请写明提醒时间，例如“两分钟后提醒我喝水”或“明天上午九点提醒我开会”。")
        return None
    due, remaining = result
    if setup and _OTHER_PERSON.match(remaining.strip(_TRIM)):
        raise ReminderParseError("目前只能提醒你自己，请使用“提醒我”；暂不支持替其他人创建提醒。")
    if not setup and intent and intent.start() > 0:
        if not _INTENT.match(remaining.strip(_TRIM)):
            return None
    return due, _content(remaining)

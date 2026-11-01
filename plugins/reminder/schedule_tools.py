"""Validation for model-extracted schedules; times and limits stay server-side."""
from __future__ import annotations

import re
from datetime import datetime

from .reminder_parser import (
    _number, _DURATION_PART, _UNITS, _NEGATIVE, _REPORTED, _DISCUSSION,
    _STATUS_QUESTION, _RECURRENCE,
)

TOOL_NAME = "create_schedule_reminders"
_REMINDER_NOUN = r"(?:提醒(?:事项|任务)?|通知|闹钟)"
_SETUP_ACTION = r"(?:创建|设置|设|安排|添加|加|定|建立|做)"
_SELF_ACTION = r"(?:提醒|通知|叫|喊)(?:一下|下|一声)?我"
_SCHEDULE_SETUP = re.compile(
    r"(?:日程|时间表|行程).{0,20}(?:帮我|给我|为我).{0,8}"
    r"(?:设(?:置)?|安排|创建|添加|定)(?:一下)?(?:提醒|通知|闹钟)?"
)
# Asking whether this functionality exists is different from requesting a
# concrete action. A modal request with “帮我/给我” remains an instruction.
_CAPABILITY_QUESTION = re.compile(
    rf"(?:能|可以|能够|会|支持).{{0,16}}{_REMINDER_NOUN}(?:吗|么|嘛)[？?。]*$"
)
_SCHEDULE_QUESTION = re.compile(
    r"(?:什么|哪些|哪几个|有|有无|有没有|有什么).{0,16}"
    rf"{_REMINDER_NOUN}.*(?:吗|么|嘛|[？?])\s*$"
)
_SCHEDULE_LOOKUP = re.compile(
    rf"(?:图|图片|照片|截图).{{0,24}}(?:有哪些|有什么|哪些|是什么).{{0,12}}"
    rf"{_REMINDER_NOUN}\s*(?:吗|么|嘛|[？?])?\s*$"
)
_LEAD_START = re.compile(r"提前|提早|(?<!提)前(?=\s*(?:\d|[零〇一二两三四五六七八九十百千万半]))")
_LEAD_JOIN = re.compile(r"^\s*(?:又|再|和|或|或者|到|至|[-—~～])\s*(?:\d|[零〇一二两三四五六七八九十百千万半])")
_RESCHEDULE_PATTERNS = (
    # “改到/换成明天相同时间”“调整到后天按原时间提醒我”
    re.compile(r"^(?:改|换|调整|挪|安排)(?:到|成)?\s*(明天|后天)(?:的)?\s*"
               r"(?:(?:相同|同样|同一|一样)\s*(?:时间|时刻|安排)|按原时间|照旧|"
               r"提醒(?:我)?|创建提醒|安排提醒)"
               r"(?:提醒(?:我)?|创建提醒|安排提醒)?$"),
    # “按照明天相同时间提醒我”“按后天同一时间”
    re.compile(r"^(?:按|按照|照着)\s*(明天|后天)(?:的)?\s*"
               r"(?:相同|同样|同一|一样)\s*(?:时间|时刻|安排)"
               r"(?:提醒我|创建提醒|安排提醒)?$"),
    # “明天相同时间提醒我”“后天按原时间提醒”
    re.compile(r"^(明天|后天)(?:的)?\s*(?:(?:相同|同样|同一|一样)\s*"
               r"(?:时间|时刻|安排)|按原时间|照旧)(?:提醒(?:我)?|创建提醒|安排提醒)?$"),
    # Confirmations commonly sent after a failed schedule.
    re.compile(r"^(?:对|好|好的|可以|是的|那就|就)[，,\s]*(明天|后天)"
               r"(?:(?:按原时间|照旧|相同时间|同一时间)?提醒我|"
               r"(?:的)?\s*(?:相同|同样|同一|一样)?\s*(?:时间|时刻|安排)"
               r"(?:提醒我)?|提醒|创建提醒|安排提醒)?$"),
    # An explicit confirmation can omit the repeated verb: “那就明天吧”。
    re.compile(r"^(?:那就|就|好的|好|对)[，,\s]*(明天|后天)(?:吧)?$"),
)
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "minItems": 1, "maxItems": 10, "items": {
            "type": "object", "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 500},
                "event_at": {"type": "string", "description": "日程原定时间，带时区的完整 ISO8601，例如 2026-10-28T18:00:00+08:00。不是提前后的时间。"},
            }, "required": ["text", "event_at"], "additionalProperties": False,
        }},
        "advance_minutes": {"type": "integer", "minimum": 0, "maximum": 10080,
                            "description": "用户要求提前的分钟数；工具会从日程原定时间扣除，未要求提前填0。"},
    },
    "required": ["items", "advance_minutes"], "additionalProperties": False,
}


def schedule_intent(text: str) -> bool:
    """Only direct instructions authorize the tool; never quoted image text."""
    text = text.strip()
    if not text or text.startswith(("/", "“", "‘", '"', "'", ">", "```", "「", "『")):
        return False
    if any(pattern.search(text) for pattern in (_NEGATIVE, _REPORTED, _DISCUSSION, _STATUS_QUESTION, _RECURRENCE)):
        return False
    if re.search(r'[“「『"].*(?:提醒|通知).*?[”」』"]', text):
        return False
    if re.fullmatch(r"(?:请问)?(?:你)?(?:能|可以|会|能够)(?:提醒|通知|叫|喊)我[吗嘛么？?]*", text):
        return False
    if re.search(r"(?:提醒|通知|叫|喊)(?:一下|下|一声)?(?:他|她|大家|所有人|全体|他们|她们|你们)", text):
        return False
    if re.search(r"(?:创建|设置|安排|添加|提醒).{0,12}(?:了吗|了么|成功了吗|有没有)[？?。]*$", text):
        return False
    if _CAPABILITY_QUESTION.search(text) and not re.search(r"(?:帮我|给我|为我)", text):
        return False
    if _SCHEDULE_QUESTION.search(text) and not re.search(r"(?:帮我|给我|为我)", text):
        return False
    if _SCHEDULE_LOOKUP.search(text) and not re.search(r"(?:创建|设置|安排|添加|定|提前|提早|叫我)", text):
        return False
    # Both self-directed actions and explicit setup requests authorize a
    # reminder. Word order may vary around a confirmed schedule or picture.
    direct = bool(
        re.search(_SELF_ACTION, text)
        or re.search(rf"{_SETUP_ACTION}.{{0,32}}{_REMINDER_NOUN}", text)
        or _SCHEDULE_SETUP.search(text)
    )
    if direct:
        return True
    # Image/schedule requests often put the object before the action (“把图上
    # 的每个时间提前十分钟提醒”). Keep this bounded and require both a
    # schedule context and an action word so “这张图是什么” is untouched.
    has_context = bool(re.search(r"图|图片|照片|截图|日程|时间表|行程", text))
    has_action = bool(re.search(
        r"(?:提醒|通知|叫我|闹钟|创建|设置|安排|添加|定制|提前|提早|前\s*(?:\d|[零〇一二两三四五六七八九十百千万半]))",
        text,
    ))
    return has_context and has_action


def reschedule_followup(text: str) -> int | None:
    """Return the day offset for a direct follow-up to a failed schedule.

    This parser intentionally recognizes only a narrow continuation form.  It
    must be paired with a server-side pending schedule scoped to the same
    UMO/sender; the phrase alone never authorizes creating a reminder.
    """
    text = (text or "").strip().strip("，,。；;：:！!~～ ")
    if not text or re.search(r"(?:吗|么|嘛)[？?。！!]*$|[？?]$", text):
        return None
    for pattern in _RESCHEDULE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            return {"明天": 1, "后天": 2}[match.group(1)]
    return None


def uses_schedule_context(text: str) -> bool:
    return schedule_intent(text) and bool(re.search(r"图|照片|截图|日程|时间表|行程|提前|提早|前\s*(?:\d|[零〇一二两三四五六七八九十百千万半])", text))


def requested_lead(text: str) -> int:
    markers = list(_LEAD_START.finditer(text))
    if not markers:
        return 0
    if len(markers) != 1:
        raise ValueError("请明确统一提前多少分钟，例如“提前十分钟提醒我”。")
    remaining = text[markers[0].end():]
    # Consume a duration as a unit sequence, which naturally covers
    # “一个半小时/一小时半/一小时三十分钟/1.5小时” without guessing.
    remaining = re.sub(r"^\s*(?:时间(?:为|是)?|为|是)?\s*", "", remaining)
    seconds = 0.0
    consumed = False
    while True:
        part = _DURATION_PART.match(remaining.lstrip())
        if not part:
            break
        consumed = True
        seconds += (_number(part["number"]) + (0.5 if part["half"] or part["prehalf"] else 0)) * _UNITS[part["unit"].lower()]
        remaining = remaining.lstrip()[part.end():]
    if not consumed:
        raise ValueError("请明确统一提前多少分钟，例如“提前十分钟提醒我”。")
    # Numeric leftovers mean an incomplete or alternative duration, not text
    # the tool can silently discard (e.g. “10分钟或20分钟”, “1.5”).
    if re.match(r"\s*(?:\d|[零〇一二两三四五六七八九十百千万半点.])", remaining) or _LEAD_JOIN.match(remaining):
        raise ValueError("提前时间有歧义，请给出一个完整时长，例如“提前90分钟提醒我”。")
    minutes = seconds / 60
    if not minutes.is_integer() or not 0 <= minutes <= 10080:
        raise ValueError("提前时间需要是0至10080之间的整数分钟。")
    return int(minutes)


def validate_items(items, advance_minutes, *, text: str, now: float,
                   min_delay: float, max_delay: float, max_text: int):
    if type(advance_minutes) is not int or advance_minutes != requested_lead(text):
        raise ValueError("提前分钟数与用户原始要求不一致，请使用用户明确指定的提前时间。")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("一次请提供1至10条日程。")
    parsed = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"text", "event_at"}:
            raise ValueError("每条日程必须包含 text 和完整原定时间 event_at。")
        content, when = item["text"], item["event_at"]
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= max_text:
            raise ValueError(f"每条提醒内容需要1至{max_text}个字符。")
        if re.search(r"<\s*(?:qqbot-|@)|@(?:everyone|here)", content, re.I):
            raise ValueError("提醒内容不能包含额外的@标签，只能提醒当前用户。")
        if not isinstance(when, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)", when):
            raise ValueError("日期、时间或时区不完整，请先向用户澄清；不能猜测日期。")
        try:
            start = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise ValueError("日程日期或时间无效，请核对图片。") from exc
        due = start - advance_minutes * 60
        if due - now < min_delay:
            raise ValueError("提前后的提醒时间已过或太近，请向用户说明并确认新的提醒时间，不要自动改到明天。")
        if due - now > max_delay:
            raise ValueError("提醒时间不能超过7天，请缩小日程范围。")
        key = (due, content.strip())
        if key not in seen:
            seen.add(key)
            parsed.append((due, content.strip(), start))
    return parsed

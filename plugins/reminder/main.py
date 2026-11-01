from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import ProviderRequest

from .reminder_parser import ReminderParseError, parse_due_many, parse_natural_many
from .schedule_tools import (
    TOOL_NAME, TOOL_SCHEMA, reschedule_followup, schedule_intent,
    uses_schedule_context, validate_items,
)

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# Fail closed. The deployed plugin config should contain the exact UMO(s) that
# have been approved for the QQ bot. A UMO includes the platform and group id.
DEFAULT_ALLOWED_UMOS: set[str] = set()
REMINDER_FILE = "/var/lib/astrbot/data/plugins/reminder/reminders.json"
MAX_DELAY_SECONDS = 7 * 24 * 60 * 60
MIN_DELAY_SECONDS = 5
MAX_TEXT_CHARS = 500
MAX_PER_GROUP = 20
MAX_PER_SENDER = 10
PENDING_SCHEDULE_TTL = 30 * 60


class AddressedGroup(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        # Generic listeners also set is_wake, so only trust the explicit
        # mention/wake flag established by AstrBot before plugin matching.
        return bool(event.is_at_or_wake_command)


def _request_text(event: AstrMessageEvent) -> str:
    # Never execute instructions embedded in forwarded/quoted message chains.
    return "".join(part.text for part in event.get_messages() if isinstance(part, Plain)).strip()


def _image_schedule_candidate(event: AstrMessageEvent, text: str) -> bool:
    """Recognize an attached-image schedule request when the text omits “图”.

    The candidate only widens tool availability. ``create_schedule_reminders``
    still validates every extracted timestamp, lead time, permission and
    horizon before persisting anything.
    """
    if not text or re.search(r"(?:不要|不用|无需|不必|取消|撤销|怎么|如何|为什么|吗|么|嘛|[？?])", text):
        return False
    has_image = any("image" in type(part).__name__.lower() for part in event.get_messages())
    if not has_image:
        return False
    return bool(re.search(r"(?:提醒我|通知我|叫我|提前|提早|闹钟|创建|设置|安排|添加|定时)", text))


@star.register(
    name="reminder",
    author="local",
    desc="本地持久化 QQ 群定时提醒，不经过 Codex",
    version="1.3.3",
)
class Main(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        cfg = config or {}
        self.allowed_umos = {str(x).strip() for x in (cfg.get("allowed_umos") or DEFAULT_ALLOWED_UMOS) if str(x).strip()}
        self.allowed_senders = {str(x).strip() for x in (cfg.get("allowed_senders") or []) if str(x).strip()}
        self.reminder_file = Path(str(cfg.get("reminder_file") or REMINDER_FILE))
        self.max_delay = min(max(int(cfg.get("max_delay_seconds", MAX_DELAY_SECONDS)), 60), MAX_DELAY_SECONDS)
        self.min_delay = min(max(int(cfg.get("min_delay_seconds", MIN_DELAY_SECONDS)), 1), 3600)
        self.max_text_chars = min(max(int(cfg.get("max_text_chars", MAX_TEXT_CHARS)), 10), MAX_TEXT_CHARS)
        self.max_per_group = min(max(int(cfg.get("max_per_group", MAX_PER_GROUP)), 1), 100)
        self.max_per_sender = min(max(int(cfg.get("max_per_sender", MAX_PER_SENDER)), 1), 50)
        self._reminders: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None
        self._live_events: dict[str, AstrMessageEvent] = {}
        # A failed schedule is kept briefly so a follow-up such as
        # “改成明天相同时间” can safely reuse the model-extracted items and
        # lead time. This is in-memory only and scoped by UMO + sender.
        self._pending_schedules: dict[tuple[str, str], dict[str, Any]] = {}
        self._closed = False

    async def initialize(self) -> None:
        self._load()
        self._scheduler_task = asyncio.create_task(self._scheduler(), name="astrbot-reminder-scheduler")
        self._scheduler_task.add_done_callback(self._scheduler_done)
        print(f"[reminder] scheduler started: pending={len(self._reminders)} allowed_umos={sorted(self.allowed_umos)}", flush=True)

    def _scheduler_done(self, task: asyncio.Task) -> None:
        if task.cancelled() or self._closed:
            return
        error = task.exception()
        if error is not None:
            print(f"[reminder] scheduler stopped unexpectedly: {error!r}", flush=True)

    async def terminate(self) -> None:
        self._closed = True
        task = self._scheduler_task
        self._scheduler_task = None
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            self._save_locked()

    def _allowed(self, event: AstrMessageEvent) -> bool:
        if event.unified_msg_origin not in self.allowed_umos:
            return False
        sender = str(event.get_sender_id())
        return bool(sender) and (not self.allowed_senders or sender in self.allowed_senders)

    @staticmethod
    def _schedule_key(event: AstrMessageEvent) -> tuple[str, str]:
        return event.unified_msg_origin, str(event.get_sender_id())

    def _pending_schedule(self, event: AstrMessageEvent) -> dict[str, Any] | None:
        key = self._schedule_key(event)
        pending = self._pending_schedules.get(key)
        if pending is None:
            return None
        if time.time() - float(pending.get("created_at", 0)) >= PENDING_SCHEDULE_TTL:
            self._pending_schedules.pop(key, None)
            return None
        return pending

    def _remember_pending_schedule(self, event: AstrMessageEvent, items: Any,
                                   advance_minutes: Any, source_text: str) -> None:
        # Store only the bounded, schema-shaped data needed for a date shift.
        if not isinstance(items, list) or not 1 <= len(items) <= 10 or type(advance_minutes) is not int:
            return
        safe_items = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "event_at"}:
                return
            content, event_at = item.get("text"), item.get("event_at")
            if (not isinstance(content, str) or not 1 <= len(content.strip()) <= self.max_text_chars
                    or not isinstance(event_at, str)
                    or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:Z|[+-]\d\d:\d\d)", event_at)):
                return
            safe_items.append({"text": content.strip(), "event_at": event_at})
        self._pending_schedules[self._schedule_key(event)] = {
            "items": safe_items,
            "advance_minutes": advance_minutes,
            "source_text": source_text,
            "created_at": time.time(),
        }
        self.logger.info("Stored failed schedule for follow-up items=%d", len(safe_items))

    def _shift_pending_items(self, pending: dict[str, Any], day_offset: int) -> list[dict[str, str]]:
        shifted = []
        for item in pending["items"]:
            when = datetime.fromisoformat(str(item["event_at"]).replace("Z", "+00:00"))
            shifted.append({
                "text": item["text"],
                "event_at": (when + timedelta(days=day_offset)).isoformat(timespec="seconds"),
            })
        return shifted

    def _is_pending_followup(self, event: AstrMessageEvent, text: str) -> bool:
        return self._pending_schedule(event) is not None and reschedule_followup(text) is not None

    async def _handle_pending_followup(self, event: AstrMessageEvent, day_offset: int) -> None:
        """Apply a date-shift follow-up without sending it through the LLM.

        This is shared by the natural-language and ``/提醒`` entry points so
        a prior tool refusal in the conversation can never suppress a valid
        continuation request.
        """
        pending = self._pending_schedule(event)
        if pending is None or day_offset not in {1, 2}:
            await event.send(MessageChain([Plain("提醒未创建：上一条日程上下文已失效，请重新发送图片并说明提前时间。")]))
            return
        try:
            result = await self.create_schedule_reminders(
                event, [], pending["advance_minutes"], _followup_days=day_offset,
            )
            payload = json.loads(result)
            if payload.get("ok"):
                lines = [f"已按{('明天' if day_offset == 1 else '后天')}相同时间创建 {len(payload['created'])} 条提醒："]
                for item in payload["created"]:
                    lines.append(f"{item['id']} · {item['remind_at']}（北京时间）· {item['text']}")
                await event.send(MessageChain([Plain("\n".join(lines))]))
            else:
                await event.send(MessageChain([Plain(f"提醒未创建：{payload.get('error', '日程信息无效。')}")]))
        except Exception:
            self.logger.exception("Failed to handle schedule follow-up")
            await event.send(MessageChain([Plain("提醒未创建：上一条日程上下文已失效，请重新发送图片并说明提前时间。")]))

    def _load(self) -> None:
        try:
            with self.reminder_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            reminders = data if isinstance(data, list) else data.get("reminders", [])
            self._reminders = [item for item in reminders if isinstance(item, dict) and self._valid_record(item)]
        except FileNotFoundError:
            self._reminders = []
        except Exception:
            self.logger.exception("Cannot load reminder file %s", self.reminder_file)
            self._reminders = []

    @staticmethod
    def _valid_record(item: dict[str, Any]) -> bool:
        try:
            return bool(item.get("id")) and bool(item.get("umo")) and bool(item.get("sender")) and float(item.get("due_at")) > 0 and bool(item.get("text"))
        except (TypeError, ValueError):
            return False

    def _save_locked(self) -> None:
        self.reminder_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".reminders.", dir=self.reminder_file.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._reminders, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.reminder_file)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    async def _scheduler(self) -> None:
        # Plugins initialize before platform adapters. Give QQ WebSocket time
        # to come up so overdue reminders are not discarded as "no platform".
        await asyncio.sleep(15.0)
        while not self._closed:
            due: list[dict[str, Any]] = []
            now = time.time()
            async with self._lock:
                for item in self._reminders:
                    if float(item["due_at"]) <= now and item["umo"] in self.allowed_umos:
                        due.append(item)
            for item in due:
                delivered = False
                try:
                    # QQ Official parses its <qqbot-at-user .../> markup in
                    # Markdown messages; plain-text msg_type=0 displays the
                    # markup literally.
                    chain = MessageChain(
                        [At(qq=str(item["sender"])), Plain(f"提醒：{item['text']}")],
                        use_markdown_=True,
                    )
                    sent = await self.context.send_message(str(item["umo"]), chain)
                    if sent is False:
                        raise RuntimeError("QQ platform is not available yet")
                    delivered = True
                except Exception:
                    # A reminder created from a live QQ event can still use a
                    # passive reply if proactive group messaging is unavailable.
                    live_event = self._live_events.get(str(item["id"]))
                    if live_event is not None:
                        try:
                            await live_event.send(chain)
                            delivered = True
                        except Exception:
                            pass
                    if not delivered:
                        self.logger.exception("Failed to send reminder %s; will retry", item.get("id"))
                async with self._lock:
                    current = next((value for value in self._reminders if value["id"] == item["id"]), None)
                    if current is None:
                        continue
                    if delivered:
                        self._reminders.remove(current)
                        self._live_events.pop(str(item["id"]), None)
                    else:
                        attempts = int(current.get("attempts", 0)) + 1
                        if attempts >= 5:
                            self._reminders.remove(current)
                            self._live_events.pop(str(item["id"]), None)
                        else:
                            current["attempts"] = attempts
                            current["due_at"] = time.time() + 30
                    self._save_locked()
            await asyncio.sleep(1.0)

    @filter.command("提醒", alias={"remind"}, priority=100)
    async def remind(self, event: AstrMessageEvent, body: GreedyStr):
        # Claim the explicit command even when this UMO is not authorized, so
        # it cannot fall through to AstrBot's general LLM pipeline.
        event.stop_event()
        if not self._allowed(event):
            return
        body = str(body or "").strip()
        if body in {"", "帮助", "help"}:
            await event.send(MessageChain([Plain("直接 @我说“两分钟后提醒我喝水”即可。也支持 /提醒 2分钟后喝水；/提醒列表；/提醒取消 <ID>")]))
            return
        if body in {"列表", "list"}:
            await self._list(event)
            return
        if body.startswith("取消") or body.lower().startswith("cancel"):
            await self._cancel(event, body)
            return

        # A command-form continuation must use the same deterministic path as
        # an @-message. Otherwise the old model conversation can answer that
        # the reminder tool is unavailable even though the server has a saved
        # failed schedule.
        followup_days = reschedule_followup(body)
        if self._pending_schedule(event) is not None and followup_days is not None:
            await self._handle_pending_followup(event, followup_days)
            return
        if self._pending_schedule(event) is not None and body in {
            "处理下", "继续", "按上条", "按上一条", "原来那个", "刚才那个",
        }:
            await event.send(MessageChain([Plain("上一条日程已保存。请明确说“改成明天相同时间”或“改成后天相同时间”。")]))
            return

        now = datetime.now(LOCAL_TZ)
        try:
            parsed = parse_due_many(body, now)
        except ReminderParseError as error:
            await event.send(MessageChain([Plain(str(error))]))
            return
        if not parsed:
            await event.send(MessageChain([Plain("无法解析时间。示例：/提醒 两分钟后喝水，或 /提醒 明天下午三点开会。")]))
            return
        await self._create_reminders(event, parsed)

    @filter.custom_filter(AddressedGroup, priority=90)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=90)
    async def natural_reminder(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command or not self._allowed(event):
            return
        text = _request_text(event)
        if not text or text.startswith("/"):
            return
        if self._is_pending_followup(event, text):
            # This continuation is deterministic. Handle it before the LLM so
            # an old refusal in quoted history cannot produce a false “没有
            # 提醒工具” answer.
            event.stop_event()
            day_offset = reschedule_followup(text)
            await self._handle_pending_followup(event, day_offset)
            return
        light_route = event.get_extra("intent_router_route")
        light_accepted = bool(event.get_extra("intent_router_accepted"))
        light_task = str(event.get_extra("intent_router_task") or "").strip()
        # In light mode the planner may normalize a synonym such as “到点喊我”
        # into the deterministic parser grammar. It cannot set due_at or skip
        # the parser's horizon/permission checks.
        if light_accepted and light_route == "schedule_reminder":
            return
        if uses_schedule_context(text) or _image_schedule_candidate(event, text):
            # Let the vision model extract the schedule; a guarded tool below
            # persists the result. The scalar parser cannot read image content.
            return
        parse_text = light_task if light_accepted and light_route == "reminder" and light_task else text
        try:
            parsed = parse_natural_many(parse_text, datetime.now(LOCAL_TZ))
        except ReminderParseError as error:
            if parse_text != text:
                try:
                    parsed = parse_natural_many(text, datetime.now(LOCAL_TZ))
                except ReminderParseError:
                    parsed = None
                if parsed is not None:
                    pass
                else:
                    event.stop_event()
                    await event.send(MessageChain([Plain(str(error))]))
                    return
            else:
                event.stop_event()
                await event.send(MessageChain([Plain(str(error))]))
                return
        if parsed is None:
            return
        event.stop_event()
        await self._create_reminders(event, parsed)

    @filter.on_llm_request(priority=85)
    async def provide_schedule_tool(self, event: AstrMessageEvent, req: ProviderRequest):
        text = _request_text(event)
        followup = self._is_pending_followup(event, text)
        light_schedule = bool(event.get_extra("intent_router_accepted")) and event.get_extra("intent_router_route") == "schedule_reminder"
        image_schedule = _image_schedule_candidate(event, text)
        if image_schedule:
            event.set_extra("schedule_image_candidate", True)
        if (not self._allowed(event) or event.get_platform_name() != "qq_official"
                or (not event.is_at_or_wake_command and not followup)
                or not (schedule_intent(text) or followup or light_schedule or image_schedule)):
            if self._allowed(event) and event.is_at_or_wake_command:
                self.logger.info(
                    "schedule tool skipped allowed=%s platform=%s addressed=%s intent=%s pending=%s "
                    "light=%s image=%s followup_phrase=%s",
                    self._allowed(event), event.get_platform_name(), event.is_at_or_wake_command,
                    schedule_intent(text), bool(self._pending_schedule(event)), light_schedule,
                    image_schedule, reschedule_followup(text) is not None,
                )
            return
        # Copy the tool list; adding this guarded tool must not expose it to
        # another group member's later request via a shared persona tool set.
        req.func_tool = ToolSet(list(req.func_tool.tools) if req.func_tool else [])
        req.func_tool.add_tool(FunctionTool(
            name=TOOL_NAME,
            description="根据本用户明确要求，把图片或文字中的日程批量创建为本群提醒。只提醒当前用户；必须实际调用成功才可宣称已创建。工具自动减去提前分钟数。日期不明、图片不清、循环日程先澄清，不猜测。",
            parameters=TOOL_SCHEMA,
            handler=self.create_schedule_reminders,
        ))
        self.logger.info("schedule tool injected tool=%s existing_tools=%d", TOOL_NAME, len(req.func_tool.tools))
        if not event.get_extra("schedule_tool_instructions"):
            now = datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
            instruction = (
                f"[提醒工具已可用。当前北京时间 {now}。用户要求创建提醒时，先读取本次图片/文字里的日程，"
                "调用 create_schedule_reminders，items.event_at 填日程原定时间（完整日期、时间、+08:00）；"
                "advance_minutes 填用户明确要求的提前分钟数，由工具计算实际提醒时间，禁止重复扣减。"
                "日期不明或图片难以辨认先问用户；不能根据旧回答猜测日程。一次提交所有确认的条目；"
                "只在工具返回 ok=true 后报告提醒编号与提醒时间，否则如实解释未创建原因。"
                "以本次可用工具为准，旧对话中‘没有提醒工具’的回答已过时。"
                "信息足够时调用工具，信息不完整时先澄清。]"
            )
            if followup:
                pending = self._pending_schedule(event)
                instruction += (
                    f"\n[这是失败日程的续办请求。服务器已保存 {len(pending['items'])} 条原定日程和"
                    f"提前 {pending['advance_minutes']} 分钟；用户要求改到{('明天' if reschedule_followup(text) == 1 else '后天')}相同时间。"
                    "请将每个 event_at 向后调整对应天数，并使用保存的 advance_minutes 调用工具。]"
                )
            # Refresh capabilities per request so older conversation replies
            # about missing tools do not describe the current tool set.
            req.system_prompt = (req.system_prompt or "").rstrip() + "\n" + instruction
            req.extra_user_content_parts.append(TextPart(text=instruction).mark_as_temp())
            event.set_extra("schedule_tool_instructions", True)

    async def create_schedule_reminders(self, event: AstrMessageEvent, items, advance_minutes,
                                        _followup_days: int | None = None):
        text = _request_text(event)
        pending = self._pending_schedule(event)
        followup_days = _followup_days if _followup_days in {1, 2} else (
            reschedule_followup(text) if pending is not None else None)
        followup = pending is not None and followup_days in {1, 2}
        light_schedule = bool(event.get_extra("intent_router_accepted")) and event.get_extra("intent_router_route") == "schedule_reminder"
        image_schedule = bool(event.get_extra("schedule_image_candidate"))
        if (not self._allowed(event) or event.get_platform_name() != "qq_official"
                or (not event.is_at_or_wake_command and not followup)
                or not (schedule_intent(text) or followup or light_schedule or image_schedule)):
            return json.dumps({"ok": False, "error": "本条消息没有授权创建提醒。"}, ensure_ascii=False)
        previous = event.get_extra("created_schedule_result")
        if previous:
            return previous
        if followup:
            items = self._shift_pending_items(pending, followup_days)
            advance_minutes = pending["advance_minutes"]
            validation_text = pending["source_text"]
        else:
            validation_text = text
        try:
            parsed = validate_items(items, advance_minutes, text=validation_text, now=time.time(),
                                    min_delay=self.min_delay, max_delay=self.max_delay,
                                    max_text=self.max_text_chars)
        except ValueError as error:
            if "已过或太近" in str(error) and not followup:
                self._remember_pending_schedule(event, items, advance_minutes, text)
            return json.dumps({"ok": False, "error": str(error), "created": []}, ensure_ascii=False)
        sender = str(event.get_sender_id())
        umo = event.unified_msg_origin
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        request_key = hashlib.sha256((umo + "\0" + sender + "\0" + message_id).encode()).hexdigest() if message_id else None
        async with self._lock:
            existing = [r for r in self._reminders if request_key and r.get("request_key") == request_key]
            if existing:
                created = existing
            else:
                group_count = sum(r["umo"] == umo for r in self._reminders)
                sender_count = sum(r["umo"] == umo and r["sender"] == sender for r in self._reminders)
                if group_count + len(parsed) > self.max_per_group or sender_count + len(parsed) > self.max_per_sender:
                    return json.dumps({"ok": False, "error": "本次日程超过群或个人的待发提醒数量上限，未创建任何条目。", "created": []}, ensure_ascii=False)
                created = [{"id": uuid.uuid4().hex[:8], "umo": umo, "sender": sender,
                            "due_at": due, "text": content, "event_at": start,
                            "created_at": time.time(), "request_key": request_key}
                           for due, content, start in parsed]
                old = self._reminders[:]
                self._reminders.extend(created)
                try:
                    self._save_locked()
                except Exception:
                    self._reminders = old
                    self.logger.error("Schedule reminder persistence failed")
                    return json.dumps({"ok": False, "error": "提醒保存失败，未创建，请稍后重试。", "created": []}, ensure_ascii=False)
                for item in created:
                    self._live_events[item["id"]] = event
        result = json.dumps({"ok": True, "timezone": "Asia/Shanghai", "created": [
            {"id": item["id"], "text": item["text"],
             "remind_at": datetime.fromtimestamp(item["due_at"], LOCAL_TZ).isoformat(timespec="seconds")}
            for item in created]}, ensure_ascii=False)
        event.set_extra("created_schedule_result", result)
        self._pending_schedules.pop(self._schedule_key(event), None)
        self.logger.info("Schedule reminders saved count=%d ids=%s", len(created), ",".join(r["id"] for r in created))
        return result

    @filter.command("提醒列表", priority=100)
    async def list_reminders(self, event: AstrMessageEvent):
        event.stop_event()
        if self._allowed(event):
            await self._list(event)

    @filter.command("提醒取消", priority=100)
    async def cancel_reminder(self, event: AstrMessageEvent, reminder_id: GreedyStr):
        event.stop_event()
        if self._allowed(event):
            await self._cancel(event, "取消 " + str(reminder_id or ""))

    async def _create_reminders(self, event: AstrMessageEvent, parsed: list[tuple[float, str]]) -> None:
        """Validate and persist a batch atomically; no partial creation."""
        # De-duplicate repeated clauses while preserving their first-seen order.
        # This keeps retries or copied text from consuming quota twice.
        unique: list[tuple[float, str]] = []
        seen: set[tuple[float, str]] = set()
        for item in parsed:
            key = (float(item[0]), str(item[1]).strip())
            if key not in seen:
                seen.add(key)
                unique.append((key[0], key[1]))
        parsed = unique
        if not parsed:
            return
        sender = str(event.get_sender_id())
        now = time.time()
        for due_at, text in parsed:
            if len(text) > self.max_text_chars:
                await event.send(MessageChain([Plain(f"提醒内容最多 {self.max_text_chars} 个字符；本次未创建任何提醒。")]))
                return
            delay = due_at - now
            if delay < self.min_delay:
                await event.send(MessageChain([Plain(f"提醒时间至少需要在 {self.min_delay} 秒后；本次未创建任何提醒。")]))
                return
            if delay > self.max_delay:
                await event.send(MessageChain([Plain("提醒时间不能超过 7 天；本次未创建任何提醒。")]))
                return
        async with self._lock:
            group_count = sum(1 for item in self._reminders if item["umo"] == event.unified_msg_origin)
            sender_count = sum(1 for item in self._reminders if item["umo"] == event.unified_msg_origin and item["sender"] == sender)
            count = len(parsed)
            if group_count + count > self.max_per_group:
                await event.send(MessageChain([Plain("本次提醒会超过群数量上限，未创建任何条目。")]))
                return
            if sender_count + count > self.max_per_sender:
                await event.send(MessageChain([Plain("本次提醒会超过你的数量上限，未创建任何条目。")]))
                return
            created = [{"id": uuid.uuid4().hex[:8], "umo": event.unified_msg_origin,
                        "sender": sender, "due_at": due_at, "text": text,
                        "created_at": time.time()} for due_at, text in parsed]
            old = self._reminders[:]
            self._reminders.extend(created)
            try:
                self._save_locked()
            except Exception:
                self._reminders = old
                self.logger.exception("Reminder persistence failed; rolled back batch")
                await event.send(MessageChain([Plain("提醒保存失败，未创建任何条目，请稍后重试。")]))
                return
            for item in created:
                self._live_events[item["id"]] = event
        lines = [f"已设置 {len(created)} 条提醒："]
        for item in created:
            due_local = datetime.fromtimestamp(item["due_at"], LOCAL_TZ).strftime("%m-%d %H:%M:%S")
            lines.append(f"{item['id']} · {due_local}（北京时间）· {item['text']}")
            self.logger.info("Reminder created id=%s source=%s", item["id"], event.unified_msg_origin)
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _create_reminder(self, event: AstrMessageEvent, parsed: tuple[float, str]) -> None:
        """Backward-compatible single-item helper for callers/tests."""
        await self._create_reminders(event, [parsed])

    async def _list(self, event: AstrMessageEvent) -> None:
        sender = str(event.get_sender_id())
        async with self._lock:
            own = [item for item in self._reminders if item["umo"] == event.unified_msg_origin and item["sender"] == sender]
        if not own:
            await event.send(MessageChain([Plain("你目前没有待发送的提醒。")]))
            return
        lines = ["你的提醒："]
        for item in sorted(own, key=lambda value: float(value["due_at"])):
            when = datetime.fromtimestamp(float(item["due_at"]), LOCAL_TZ).strftime("%m-%d %H:%M")
            lines.append(f"{item['id']} · {when} · {item['text']}")
        await event.send(MessageChain([Plain("\n".join(lines))]))

    async def _cancel(self, event: AstrMessageEvent, body: str) -> None:
        match = re.search(r"(?:取消|cancel)\s+([A-Za-z0-9_-]{4,32})", body, re.IGNORECASE)
        if not match:
            await event.send(MessageChain([Plain("用法：/提醒取消 <ID>。可先用 /提醒列表 查看 ID。")]))
            return
        reminder_id = match.group(1)
        sender = str(event.get_sender_id())
        async with self._lock:
            before = len(self._reminders)
            self._reminders = [item for item in self._reminders if not (item["id"] == reminder_id and item["umo"] == event.unified_msg_origin and item["sender"] == sender)]
            changed = len(self._reminders) != before
            if changed:
                self._live_events.pop(reminder_id, None)
                self._save_locked()
        await event.send(MessageChain([Plain("已取消提醒。" if changed else "未找到属于你的该提醒。")]))

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.core.star.filter.command import GreedyStr

from .reminder_parser import ReminderParseError, parse_due, parse_natural

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


class AddressedGroup(filter.CustomFilter):
    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        # Generic listeners also set is_wake, so only trust the explicit
        # mention/wake flag established by AstrBot before plugin matching.
        return bool(event.is_at_or_wake_command)


def _request_text(event: AstrMessageEvent) -> str:
    # Never execute instructions embedded in forwarded/quoted message chains.
    return "".join(part.text for part in event.get_messages() if isinstance(part, Plain)).strip()


@star.register(
    name="reminder",
    author="local",
    desc="本地持久化 QQ 群定时提醒，不经过 Codex",
    version="1.1.0",
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

        now = datetime.now(LOCAL_TZ)
        parsed = parse_due(body, now)
        if not parsed:
            await event.send(MessageChain([Plain("无法解析时间。示例：/提醒 两分钟后喝水，或 /提醒 明天下午三点开会。")]))
            return
        await self._create_reminder(event, parsed)

    @filter.custom_filter(AddressedGroup, priority=90)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=90)
    async def natural_reminder(self, event: AstrMessageEvent):
        if not event.is_at_or_wake_command or not self._allowed(event):
            return
        text = _request_text(event)
        if not text or text.startswith("/"):
            return
        light_route = event.get_extra("intent_router_route")
        light_accepted = bool(event.get_extra("intent_router_accepted"))
        light_task = str(event.get_extra("intent_router_task") or "").strip()
        # Preserve the deterministic parser's negative/quoted/ambiguous guards
        # before accepting a light-model normalization candidate.
        try:
            parsed = parse_natural(text, datetime.now(LOCAL_TZ))
        except ReminderParseError as error:
            event.stop_event()
            await event.send(MessageChain([Plain(str(error))]))
            return
        if parsed is None and light_accepted and light_route == "reminder" and light_task:
            try:
                parsed = parse_natural(light_task, datetime.now(LOCAL_TZ))
            except ReminderParseError as error:
                event.stop_event()
                await event.send(MessageChain([Plain(str(error))]))
                return
        if parsed is None:
            return
        event.stop_event()
        await self._create_reminder(event, parsed)

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

    async def _create_reminder(self, event: AstrMessageEvent, parsed: tuple[float, str]) -> None:
        due_at, text = parsed
        sender = str(event.get_sender_id())
        if len(text) > self.max_text_chars:
            await event.send(MessageChain([Plain(f"提醒内容最多 {self.max_text_chars} 个字符。")]))
            return
        delay = due_at - time.time()
        if delay < self.min_delay:
            await event.send(MessageChain([Plain(f"提醒时间至少需要在 {self.min_delay} 秒后。")]))
            return
        if delay > self.max_delay:
            await event.send(MessageChain([Plain("提醒时间不能超过 7 天。")]))
            return
        async with self._lock:
            group_count = sum(1 for item in self._reminders if item["umo"] == event.unified_msg_origin)
            sender_count = sum(1 for item in self._reminders if item["umo"] == event.unified_msg_origin and item["sender"] == sender)
            if group_count >= self.max_per_group:
                await event.send(MessageChain([Plain("这个群的提醒数量已达到上限。")]))
                return
            if sender_count >= self.max_per_sender:
                await event.send(MessageChain([Plain("你在这个群的提醒数量已达到上限。")]))
                return
            reminder_id = uuid.uuid4().hex[:8]
            self._reminders.append({"id": reminder_id, "umo": event.unified_msg_origin, "sender": sender, "due_at": due_at, "text": text, "created_at": time.time()})
            self._live_events[reminder_id] = event
            self._save_locked()
        due_local = datetime.fromtimestamp(due_at, LOCAL_TZ).strftime("%m-%d %H:%M:%S")
        self.logger.info("Reminder created id=%s source=%s", reminder_id, event.unified_msg_origin)
        await event.send(MessageChain([Plain(f"已设置提醒（{reminder_id}），将在 {due_local}（北京时间）@你：{text}")]))

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

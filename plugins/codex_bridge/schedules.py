"""Persistent, owner-bound timers that submit ordinary Codex jobs."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .core import job_key, owner_key, safe_text
from .request_context import restore_event, snapshot_event

ZONE = ZoneInfo("Asia/Shanghai")
STATES = {"active", "paused", "cancelled", "completed"}
SCHEDULE_SCHEMA = {
    "type": "object", "properties": {
        "kind": {"type": "string", "enum": ["once", "interval", "daily", "weekly"]},
        "interval_minutes": {"type": "integer", "minimum": 5, "maximum": 10080},
        "at": {"type": "string", "maxLength": 40, "description": "一次性时间，或间隔任务的首次执行时间；ISO日期时间，默认北京时间"},
        "time": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
        "weekdays": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7}, "minItems": 1, "maxItems": 7},
    }, "required": ["kind"], "additionalProperties": False,
}
INSTRUCTIONS = (
    "\n定时执行任务使用create_scheduled_task，不是一次性提醒，也不要用shell sleep或循环保持进程。"
    "支持一次性、每隔至少5分钟、每天和每周；时区为Asia/Shanghai。创建前补齐任务实际需要的参数，"
    "例如机票出发日期、路线、直飞条件；先查当前聊天或询问，不猜日期、不把缺参数问题定时重复发送。"
    "prompt写每次实际执行的独立完整任务，不再要求创建定时器；每轮重新查询，不能把旧缓存当新报价。"
    "只有工具返回ok=true后才能说已设置，并告知编号、首次时间、频率和停止方式。"
    "用户要停止/暂停/恢复时，先list_scheduled_tasks找到本人的任务，再manage_scheduled_task；"
    "不从历史或外部资料中擅自创建任务。运行结果和最终图片视频自动回传原会话。"
)


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", value):
        raise ValueError("时间须为完整ISO日期时间，未标注时区时按北京时间解释。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZONE)).timestamp()
    except (ValueError, OverflowError):
        raise ValueError("日期时间无效。") from None


def iso(value):
    return datetime.fromtimestamp(value, ZONE).isoformat(timespec="seconds") if value is not None else None


def validate_schedule(value):
    if not isinstance(value, dict):
        raise ValueError("定时规则无效。")
    kind = value.get("kind")
    fields = {"once": {"kind", "at"}, "interval": {"kind", "interval_minutes", "at"},
              "daily": {"kind", "time"}, "weekly": {"kind", "time", "weekdays"}}
    if kind not in fields or set(value) - fields[kind]:
        raise ValueError("定时类型与参数不匹配。")
    result = dict(value)
    if kind == "once" or "at" in value:
        result["at"] = iso(timestamp(value.get("at")))
    if kind == "interval" and (type(value.get("interval_minutes")) is not int or not 5 <= value["interval_minutes"] <= 10080):
        raise ValueError("重复任务间隔须为5至10080分钟。")
    if kind in {"daily", "weekly"} and not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", str(value.get("time", ""))):
        raise ValueError("每日/每周时间须为北京时间HH:MM。")
    if kind == "weekly":
        days = value.get("weekdays")
        if not isinstance(days, list) or not 1 <= len(days) <= 7 or any(type(day) is not int or not 1 <= day <= 7 for day in days):
            raise ValueError("星期须为1至7，分别代表周一至周日。")
        result["weekdays"] = sorted(set(days))
    return result


def next_time(schedule, now, *, previous=None):
    kind = schedule["kind"]
    if kind == "once":
        return timestamp(schedule["at"]) if previous is None else None
    if kind == "interval":
        interval = schedule["interval_minutes"] * 60
        anchor = previous if previous is not None else timestamp(schedule["at"]) if "at" in schedule else now + interval
        return anchor if anchor > now else anchor + (math.floor((now - anchor) / interval) + 1) * interval
    local = datetime.fromtimestamp(now, ZONE)
    hour, minute = map(int, schedule["time"].split(":"))
    for offset in range(8):
        candidate = (local + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() > now and (kind == "daily" or candidate.isoweekday() in schedule["weekdays"]):
            return candidate.timestamp()
    raise ValueError("无法计算下次执行时间。")


class Schedules:
    def __init__(self, bridge):
        self.bridge = bridge
        self.records = {}
        self._tick_lock = asyncio.Lock()

    @property
    def enabled(self):
        return self.bridge.config.get("scheduled_tasks_enabled", True) is True

    def allowed(self, event):
        return self.enabled and self.bridge._storage_ok and self.bridge._allowed(event) and bool(event.is_at_or_wake_command)

    def load(self, entries):
        if not isinstance(entries, list) or len(entries) > 200:
            raise ValueError("invalid scheduled task storage")
        records = {}
        for item in entries:
            if (not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{16}", str(item.get("id", "")))
                    or item.get("state") not in STATES or item["id"] in records):
                raise ValueError("invalid scheduled task")
            event = restore_event(item["request"])
            if (item.get("owner") != owner_key(event.unified_msg_origin, event.get_sender_id())
                    or item.get("umo") != event.unified_msg_origin or item.get("sender") != event.get_sender_id()
                    or not isinstance(item.get("prompt"), str) or not 1 <= len(item["prompt"]) <= 6000
                    or not isinstance(item.get("title"), str) or not 1 <= len(item["title"]) <= 80
                    or event.message_str != item["prompt"]
                    or type(item.get("run_count")) is not int or item["run_count"] < 0):
                raise ValueError("invalid scheduled task identity")
            if validate_schedule(item["schedule"]) != item["schedule"]:
                raise ValueError("invalid stored schedule")
            for field in ("created_at", "updated_at", "next_run_at", "end_at"):
                value = item.get(field)
                if value is None and field in {"next_run_at", "end_at"}:
                    continue
                if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                    raise ValueError("invalid stored time")
            inflight = item.get("inflight")
            if inflight is not None:
                if (not isinstance(inflight, dict) or not re.fullmatch(r"[a-f0-9]{16}", str(inflight.get("job_id", "")))
                        or type(inflight.get("at")) not in {int, float} or not math.isfinite(inflight["at"]) or inflight["at"] <= 0
                        or type(inflight.get("started_at")) not in {int, float} or not math.isfinite(inflight["started_at"]) or inflight["started_at"] <= 0
                        or type(inflight.get("attempts")) is not int or not 0 <= inflight["attempts"] <= 100000
                        or type(inflight.get("retry_at", 0)) not in {int, float} or not math.isfinite(inflight.get("retry_at", 0))
                        or inflight["job_id"] != job_key(item["umo"], item["sender"], self.message_id(item, inflight["at"]))):
                    raise ValueError("invalid scheduled run")
            records[item["id"]] = copy.deepcopy(item)
        self.records = records

    def serialized(self):
        terminal = sorted((item for item in self.records.values() if item["state"] in {"cancelled", "completed"} and not item.get("inflight")),
                          key=lambda item: item["updated_at"])
        for item in terminal[:max(0, len(self.records) - 100)]:
            self.records.pop(item["id"], None)
        return list(self.records.values())

    @staticmethod
    def public(item):
        return {"id": item["id"], "title": item["title"], "prompt": item["prompt"], "state": item["state"],
                "schedule": copy.deepcopy(item["schedule"]), "timezone": "Asia/Shanghai", "next_run_at": iso(item.get("next_run_at")),
                "end_at": iso(item.get("end_at")), "run_count": item.get("run_count", 0), "last_run": item.get("last_run"),
                "running_job_id": (item.get("inflight") or {}).get("job_id"), "last_error": item.get("last_error")}

    def tools(self):
        return [
            {"name": "create_scheduled_task", "description": "持久化创建本人原会话的定时Codex任务；到点重新执行并回传结果，不是仅发提醒。必须先补齐业务参数（如机票日期），支持一次性/间隔/每日/每周，最短间隔5分钟；不给end_at则持续直到暂停或取消。",
             "parameters": {"type": "object", "properties": {
                 "title": {"type": "string", "minLength": 1, "maxLength": 80},
                 "prompt": {"type": "string", "minLength": 1, "maxLength": 6000, "description": "每轮独立执行的完整任务，含已确认的具体日期、地点、条件；不要再次创建定时任务"},
                 "schedule": SCHEDULE_SCHEMA, "end_at": {"type": "string", "maxLength": 40, "description": "可选，完整ISO截止时间；省略表示持续到用户停止"}},
                 "required": ["title", "prompt", "schedule"], "additionalProperties": False}, "handler": self.create},
            {"name": "list_scheduled_tasks", "description": "读取本人在当前会话的定时任务、下次北京时间、最近执行/回传状态及错误，不能查看其他人的任务。",
             "parameters": {"type": "object", "properties": {}, "additionalProperties": False}, "handler": self.list_tasks},
            {"name": "manage_scheduled_task", "description": "按用户要求暂停、恢复或取消本人当前会话的定时任务；先列出取得准确编号。暂停/取消后不再启动或回传；已开始的单次计算可能仍在结束中。",
             "parameters": {"type": "object", "properties": {
                 "task_id": {"type": "string", "pattern": "^[a-f0-9]{16}$"},
                 "action": {"type": "string", "enum": ["pause", "resume", "cancel"]}},
                 "required": ["task_id", "action"], "additionalProperties": False}, "handler": self.manage},
        ]

    async def create(self, event, title, prompt, schedule, end_at=None):
        if not self.allowed(event) or event.get_extra("scheduled_task_id"):
            return {"ok": False, "code": "not_authorized"}
        try:
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80 or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 6000:
                raise ValueError("标题或任务内容为空或过长。")
            schedule = validate_schedule(schedule)
            request = snapshot_event(event, task_text=prompt.strip())
            if not request["message_id"]:
                raise ValueError("原消息编号缺失，无法防止重复创建。")
            end = timestamp(end_at) if end_at is not None else None
            fingerprint = json.dumps([request["owner"], request["message_id"], title.strip(), prompt.strip(), schedule, end], ensure_ascii=False, sort_keys=True)
            task_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
            async with self.bridge._lock:
                if task_id in self.records:
                    return {"ok": True, "created": False, "task": self.public(self.records[task_id])}
                now = time.time()
                first = timestamp(schedule["at"]) if "at" in schedule else next_time(schedule, now)
                if first < now + 5 or end is not None and end <= first:
                    raise ValueError("首次执行须至少在5秒后，截止时间须晚于首次执行。")
                active = [item for item in self.records.values() if item["state"] in {"active", "paused"}]
                if (len(active) >= 100 or len(self.records) >= 200
                        or sum(item["umo"] == request["umo"] for item in active) >= 20
                        or sum(item["owner"] == request["owner"] for item in active) >= 5):
                    return {"ok": False, "code": "schedule_quota", "error": "定时任务已达上限（每人每会话5个、每会话20个）；请先取消不需要的任务。"}
                request.update(parts=[{"type": "Plain", "text": prompt.strip()}], extras={}, awake=True)
                item = {"id": task_id, "owner": request["owner"], "sender": request["sender"], "umo": request["umo"],
                        "title": title.strip(), "prompt": prompt.strip(), "schedule": schedule, "request": request,
                        "state": "active", "created_at": now, "updated_at": now, "next_run_at": first,
                        "end_at": end, "run_count": 0, "inflight": None, "last_error": None}
                self.records[task_id] = item
                try:
                    self.bridge._save()
                except Exception:
                    self.records.pop(task_id, None)
                    raise
                return {"ok": True, "created": True, "task": self.public(item),
                        "notice": "已持久化；每轮实际运行需要时间，不保证整点完成。原会话主动推送受QQ平台权限限制。"}
        except (ValueError, OSError, RuntimeError) as error:
            return {"ok": False, "code": "invalid_schedule" if isinstance(error, ValueError) else "schedule_storage_unavailable",
                    "error": str(error) if isinstance(error, ValueError) else "定时任务未能持久化，未确认创建。"}

    async def list_tasks(self, event):
        if not self.allowed(event):
            return {"ok": False, "code": "not_authorized"}
        owner = owner_key(event.unified_msg_origin, str(event.get_sender_id()))
        async with self.bridge._lock:
            items = sorted((item for item in self.records.values() if item["owner"] == owner), key=lambda item: item["created_at"], reverse=True)
            return {"ok": True, "tasks": [self.public(item) for item in items], "observed_at": iso(time.time())}

    async def manage(self, event, task_id, action):
        if not self.allowed(event) or event.get_extra("scheduled_task_id"):
            return {"ok": False, "code": "not_authorized"}
        async with self.bridge._lock:
            item = self.records.get(task_id)
            if item is None or item["owner"] != owner_key(event.unified_msg_origin, str(event.get_sender_id())):
                return {"ok": False, "code": "schedule_not_found", "error": "未找到属于你在当前会话的定时任务。"}
            if action not in {"pause", "resume", "cancel"}:
                return {"ok": False, "code": "invalid_action"}
            state = {"pause": "paused", "resume": "active", "cancel": "cancelled"}[action]
            if state == item["state"]:
                return {"ok": True, "changed": False, "task": self.public(item)}
            if item["state"] in {"completed", "cancelled"}:
                return {"ok": False, "code": "schedule_finished", "error": "任务已结束；需要再次执行时请新建。"}
            now = time.time()
            following = next_time(item["schedule"], now) if action == "resume" else None
            if following is not None and (following <= now or item.get("end_at") is not None and following >= item["end_at"]):
                return {"ok": False, "code": "schedule_expired", "error": "原定时间已过或超过截止时间，请新建任务。"}
            previous = copy.deepcopy(item)
            route = self.bridge.routes.get((item.get("inflight") or {}).get("job_id"))
            suppressed = route.get("suppress_delivery") if route else None
            item.update(state=state, next_run_at=following, updated_at=now)
            if route and state != "active":
                route["suppress_delivery"] = True
            elif state != "active":
                item["inflight"] = None
            try:
                self.bridge._save()
            except Exception:
                self.records[task_id] = previous
                if route:
                    if suppressed is None:
                        route.pop("suppress_delivery", None)
                    else:
                        route["suppress_delivery"] = suppressed
                return {"ok": False, "code": "schedule_storage_unavailable", "error": "操作未能保存，未确认已停止。"}
            return {"ok": True, "changed": True, "task": self.public(item),
                    "notice": "暂停/取消后不再启动或回传；已经开始的单次计算可能仍在结束中，已发送的消息不能撤回。"}

    @staticmethod
    def message_id(item, at):
        return "scheduled:" + item["id"] + ":" + format(at, ".6f")

    def permits(self, route, *, now=None):
        item = self.records.get(route.get("schedule_id"))
        current = time.time() if now is None else now
        return bool(self.enabled and self.bridge._storage_ok and item and item["state"] == "active"
                    and self.bridge._route_allowed(item) and (item.get("inflight") or {}).get("job_id") == route.get("id")
                    and not route.get("suppress_delivery") and (item.get("end_at") is None or current < item["end_at"]))

    def can_submit(self, task_id, event):
        item = self.records.get(task_id)
        message = getattr(event, "message_obj", None)
        jid = job_key(event.unified_msg_origin, str(event.get_sender_id()), str(getattr(message, "message_id", "")))
        return bool(item and event.get_extra("scheduled_task_id") == task_id
                    and item["owner"] == owner_key(event.unified_msg_origin, str(event.get_sender_id()))
                    and self.permits(self.bridge.routes.get(jid, {"id": jid, "schedule_id": task_id})))

    def event(self, item):
        inflight = item["inflight"]
        request = copy.deepcopy(item["request"])
        request.update(message_id=self.message_id(item, inflight["at"]), received_at=inflight["started_at"],
                       extras={"codex_first_submitted": True, "scheduled_task_id": item["id"],
                               "current_request_received_at": inflight["started_at"]})
        return restore_event(request)

    def prompt(self, item):
        return ("[用户已创建的定时任务，本轮只执行一次，不要创建新的定时器或循环]\n"
                f"任务：{item['title']}\n计划时间：{iso(item['inflight']['at'])}；本轮启动：{iso(item['inflight']['started_at'])}\n"
                "每轮独立重新查询；必须标明实际查询时间、来源和失败，不把历史或缓存当最新结果。"
                "结果自动回传原会话，不自行发送；最终图片/视频放output/，不要重复承诺创建定时任务。\n\n"
                + item["prompt"])

    async def tick(self):
        if not self.enabled or not self.bridge._storage_ok:
            return
        async with self._tick_lock:
            for task_id in list(self.records):
                async with self.bridge._lock:
                    item = self.records.get(task_id)
                    if not item or not self.bridge._route_allowed(item):
                        continue
                    now = time.time()
                    inflight = item.get("inflight")
                    route = self.bridge.routes.get(inflight["job_id"]) if inflight else None
                    if inflight and route and route.get("acked"):
                        item.update(inflight=None, updated_at=now, run_count=item.get("run_count", 0) + 1,
                                    last_run={"job_id": inflight["job_id"], "scheduled_at": iso(inflight["at"]),
                                              "finished_at": iso(now), "state": route.get("terminal_state", "unknown"),
                                              "delivery": "suppressed" if route.get("suppress_delivery") else "delivered" if route.get("delivered") else "failed"})
                        if item["state"] == "active":
                            item["next_run_at"] = next_time(item["schedule"], now, previous=inflight["at"])
                            if item["next_run_at"] is None:
                                item["state"] = "completed"
                            elif route.get("failure_acked") and not route.get("suppress_delivery"):
                                item.update(state="paused", next_run_at=None, last_error="QQ回传多次失败，已暂停；确认主动推送权限后可恢复。")
                        self.bridge._save()
                        continue
                    if item["state"] != "active":
                        continue
                    if item.get("end_at") is not None and now >= item["end_at"]:
                        item.update(state="completed", next_run_at=None, updated_at=now)
                        if route:
                            route["suppress_delivery"] = True
                        self.bridge._save()
                        continue
                    if inflight:
                        if route and route.get("schedule_submitted") or now < inflight.get("retry_at", 0):
                            continue
                        if route:
                            try:
                                observed = await self.bridge._rpc({"op": "status", "owner": item["owner"], "job_id": inflight["job_id"]})
                            except Exception:
                                continue
                            job = observed.get("job")
                            if observed.get("ok") and isinstance(job, dict) and job.get("id") == inflight["job_id"]:
                                route["schedule_submitted"] = True
                                item["last_error"] = None
                                self.bridge._save()
                                continue
                            if observed.get("error") != "job not found":
                                continue
                        if now - inflight["started_at"] > 86400:
                            item.update(state="paused", next_run_at=None, updated_at=now,
                                        last_error="上次提交长时间未确认，已暂停以避免重复执行；请核对最近任务后再恢复。")
                            if route:
                                route["suppress_delivery"] = True
                            self.bridge._save()
                            continue
                    else:
                        if item.get("next_run_at") is None or now < item["next_run_at"]:
                            continue
                        at = item["next_run_at"]
                        inflight = {"at": at, "started_at": now, "attempts": 0,
                                    "job_id": job_key(item["umo"], item["sender"], self.message_id(item, at))}
                        item["inflight"] = inflight
                    inflight["attempts"] += 1
                    inflight["retry_at"] = now + min(300, 30 * inflight["attempts"])
                    item["updated_at"] = now
                    self.bridge._save()
                    event, prompt = self.event(item), self.prompt(item)
                result = await self.bridge._submit(event, prompt, notify=False, schedule_id=task_id)
                async with self.bridge._lock:
                    item = self.records[task_id]
                    if item["state"] == "active" and item.get("inflight"):
                        item["last_error"] = None if result.get("ok") else "本轮提交未确认，正在用同一任务编号重试，不会重复创建。"
                        self.bridge._save()

    @staticmethod
    def result_text(route, job):
        result = job.get("result") if job.get("state") == "completed" else "本轮任务执行失败，未获得可用结果；请查看任务状态，不把失败当作没有新信息。"
        return safe_text(f"【定时任务 #{route['schedule_id']}】{route.get('schedule_title', '')}\n"
                         f"本轮计划时间：{iso(route['scheduled_at'])}\n\n{result or '本轮没有可回传结果。'}\n\n"
                         f"本轮任务：/codex status {route['id']}\n停止：/定时任务停止 {route['schedule_id']}", 6000)

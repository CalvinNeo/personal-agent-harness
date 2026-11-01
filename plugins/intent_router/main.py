from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain

from .core import PlannerConfig, PlannerError, RoutePlan, build_planner_prompt, extract_plan


_PLAN_EXTRA = "intent_router_plan"
_ROUTE_EXTRA = "intent_router_route"
_ALLOWED_MODES = {"regex", "light"}
_ATTEMPT_EXTRA = "intent_router_attempted"
_MAX_PENDING = 2

# This text is deliberately static. The planner can select a capability, but it
# cannot invent a plugin name, URL, file path, sender, or permission.
_ROUTE_HINTS = {
    "reminder": "当前消息可能是自然语言提醒；让现有提醒插件从原文解析时间和内容，解析失败就澄清，不要猜时间。",
    "schedule_reminder": "当前消息可能要求从图片或文字日程创建提醒；让现有日程提醒工具读取本次上下文并做最终时间、数量和权限校验。",
    "image": "当前消息可能需要图片理解；让现有图片上下文和视觉能力处理本次可用图片，缺图时请用户重发。",
    "codex": "当前消息可能需要复杂任务分派；仅使用现有 Codex 分派能力，默认只读，继续执行会话、发送者和外部上下文校验。",
    "web_search": "当前消息可能需要网页搜索；仅使用已配置的搜索能力，先确认查询范围和安全边界。",
    "xhs_search": "当前消息可能要求搜索小红书；仅使用已配置的只读小红书搜索能力，遵守会话白名单，不执行发布、评论、点赞或收藏。",
    "xhs_status": "用户要检查小红书真实登录态。使用 xhs_login_status；不要调用 Codex、索要截图或建议开启 Computer Use。",
    "xhs_login": "用户要小红书扫码登录。由已授权私聊的小红书插件提供二维码，不接收 Token 或 Cookie。",
    "travel_search": "用户要查询/筛选高铁或机票，使用 travel_search_tickets 或 travel_filter_tickets；先澄清缺失日期和起终点，保留真实价格/余票来源，不用Codex、不下单。",
    "map_route": "用户要地点/地图导航，使用 travel_map 返回官方地图入口；当前不是实时路况API，不能编造距离、耗时或定位，不用Codex。",
}


def _plain_text(event: AstrMessageEvent) -> str:
    parts = []
    for part in event.get_messages():
        text = part.text if isinstance(part, Plain) else None
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts).strip()


def _context_hints(event: AstrMessageEvent) -> list[str]:
    hints = ["sender", "timezone", "current_time"]
    for part in event.get_messages():
        name = type(part).__name__.lower()
        if name in {"image", "imageurl", "imageurlpart"} or "image" in name:
            hints.append("images")
        if name in {"reply", "forward", "node"} or any(token in name for token in ("reply", "forward", "node")):
            hints.append("conversation")
    # The core validates the vocabulary and removes duplicates from the prompt.
    return hints


def _has_external_context(event: AstrMessageEvent) -> bool:
    for part in event.get_messages():
        name = type(part).__name__.lower()
        if any(token in name for token in ("image", "reply", "forward", "node", "file", "video", "record", "attachment")):
            return True
    return False


@star.register(
    name="intent_router",
    author="local",
    desc="两档自然语言路由：现有正则或可配置的 light 模型",
    version="0.1.2",
)
class Main(star.Star):
    """An optional, side-effect-free planner in front of existing plugins.

    ``regex`` leaves the current routing path untouched. ``light`` calls a
    dedicated provider and stores a validated plan on the current event. The
    plan is only a bounded hint for the normal LLM request; existing plugins
    remain responsible for all side effects and authorization.
    """

    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        self._pending: set[asyncio.Task] = set()
        self._closing = False
        cfg = dict(config or {})
        mode = str(cfg.get("mode") or "regex").strip().lower()
        self.mode = mode if mode in _ALLOWED_MODES else "regex"
        if mode not in _ALLOWED_MODES:
            self.logger.warning("intent_router invalid mode=%s; using regex", mode[:32])
        self.allowed_umos = {str(value).strip() for value in (cfg.get("allowed_umos") or []) if str(value).strip()}
        self.allowed_senders = {str(value).strip() for value in (cfg.get("allowed_senders") or []) if str(value).strip()}
        self.log_route_details = bool(cfg.get("log_route_details", True))
        self._provider_cfg = {
            "mode": self.mode,
            "provider_id": str(cfg.get("provider_id") or "planner-minimax-light"),
            "model": str(cfg.get("model") or "MiniMax-M3.1-Flash"),
            "timeout": cfg.get("timeout_seconds", cfg.get("timeout", 2.0)),
            "min_confidence": cfg.get("min_confidence", 0.85),
            "max_input_chars": cfg.get("max_input_chars", 6000),
        }
        try:
            self.planner_config = PlannerConfig.from_mapping(self._provider_cfg)
        except PlannerError as error:
            self.logger.error("intent_router invalid planner config code=%s; using regex", error.code)
            self.mode = "regex"
            self._provider_cfg["mode"] = "regex"
            self.planner_config = PlannerConfig.from_mapping(self._provider_cfg)

    def _allowed(self, event: AstrMessageEvent) -> bool:
        platform_getter = getattr(event, "get_platform_name", None)
        if platform_getter is not None and platform_getter() != "qq_official":
            return False
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo or umo not in self.allowed_umos:
            return False
        sender = str(event.get_sender_id() or "").strip()
        return bool(sender) and (not self.allowed_senders or sender in self.allowed_senders)

    @staticmethod
    def _is_addressed(event: AstrMessageEvent) -> bool:
        return bool(getattr(event, "is_at_or_wake_command", False))

    async def _resolve_provider(self):
        provider_id = self.planner_config.provider_id
        # Newer AstrBot exposes a sync Context getter; some 4.29 builds expose
        # the async provider-manager method instead. Support both without
        # assuming either one is available.
        getter = getattr(self.context, "get_provider_by_id", None)
        if getter is not None:
            value = getter(provider_id)
            return await value if inspect.isawaitable(value) else value
        manager = getattr(self.context, "provider_manager", None)
        getter = getattr(manager, "get_provider_by_id", None)
        if getter is not None:
            value = getter(provider_id)
            return await value if inspect.isawaitable(value) else value
        return None

    async def _request_plan(self, prompt: str):
        provider = await self._resolve_provider()
        if provider is None or not hasattr(provider, "text_chat"):
            raise PlannerError("planner provider is unavailable", code="provider_unavailable", retryable=True)
        return await provider.text_chat(prompt=prompt, model=self.planner_config.model, request_max_retries=1)

    def _request_finished(self, task: asyncio.Task):
        self._pending.discard(task)
        if not task.cancelled():
            task.exception()

    def _record_timeout(self, event: AstrMessageEvent, binding: dict, started: float):
        event.set_extra("intent_router_timeout", binding)
        self._log_failure("timeout", started)

    async def _classify(self, event: AstrMessageEvent) -> RoutePlan | None:
        if self.mode != "light" or not self._allowed(event) or not self._is_addressed(event):
            return None
        if event.get_extra(_PLAN_EXTRA) is not None:
            return event.get_extra(_PLAN_EXTRA)
        if event.get_extra(_ATTEMPT_EXTRA):
            return None
        text = _plain_text(event)
        owner = hashlib.sha256((str(event.unified_msg_origin) + "\0" + str(event.get_sender_id() or "")).encode()).hexdigest()
        if event.get_extra("long_term_memory_request") == {"event": id(event), "owner": owner, "text": hashlib.sha256(text.encode()).hexdigest()}:
            return None
        if text.startswith("/"):
            return None
        if not text:
            if not _has_external_context(event):
                return None
            text = "[当前消息没有文字，但包含需要理解的附件或图片]"
        event.set_extra(_ATTEMPT_EXTRA, True)
        binding = {"event": id(event),
                   "owner": hashlib.sha256((str(event.unified_msg_origin) + "\0" + str(event.get_sender_id() or "")).encode()).hexdigest(),
                   "text": hashlib.sha256(_plain_text(event).encode()).hexdigest()}
        started = time.monotonic()
        task = None
        try:
            if self._closing or len(self._pending) >= _MAX_PENDING:
                self._log_failure("busy", started)
                return None
            if len(text) > self.planner_config.max_input_chars:
                text = text[: self.planner_config.max_input_chars]
            prompt = build_planner_prompt(text, _context_hints(event))
            budget = float(self.planner_config.timeout)
            task = asyncio.create_task(self._request_plan(prompt))
            self._pending.add(task)
            task.add_done_callback(self._request_finished)
            completed, _ = await asyncio.wait({task}, timeout=max(0, budget - (time.monotonic() - started)))
            if not completed or time.monotonic() - started >= budget:
                task.cancel()
                self._record_timeout(event, binding, started)
                return None
            if task.cancelled():
                return None
            response = task.result()
            raw = getattr(response, "completion_text", "")
            plan = extract_plan(raw, timeout=max(0.001, budget - (time.monotonic() - started)))
            if time.monotonic() - started >= budget:
                self._record_timeout(event, binding, started)
                return None
            if plan.route == "codex":
                plan = RoutePlan(plan.route, plan.confidence, text, plan.mode, plan.missing_context)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            event.set_extra(_PLAN_EXTRA, plan.to_mapping())
            event.set_extra(_ROUTE_EXTRA, plan.route)
            # Existing plugins consume only this boolean and the bounded task
            # text; they never trust the model with permissions or due_at.
            event.set_extra("intent_router_accepted", plan.confidence >= self.planner_config.min_confidence)
            event.set_extra("intent_router_task", plan.task)
            if self.log_route_details:
                self.logger.info(
                    "intent_router mode=light route=%s confidence=%.3f latency_ms=%d accepted=%s",
                    plan.route,
                    plan.confidence,
                    elapsed_ms,
                    plan.confidence >= self.planner_config.min_confidence,
                )
            return plan
        except asyncio.CancelledError:
            if task is not None:
                task.cancel()
            raise
        except asyncio.TimeoutError:
            self._record_timeout(event, binding, started)
        except PlannerError as error:
            if error.code == "timeout":
                self._record_timeout(event, binding, started)
            else:
                self._log_failure(error.code, started)
        except Exception as error:  # provider failures must fall back silently
            self.logger.warning("intent_router planner failure type=%s", type(error).__name__)
        return None

    def _log_failure(self, code: str, started: float | None = None) -> None:
        if self.log_route_details:
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000)) if started is not None else 0
            self.logger.info("intent_router mode=light result=fallback code=%s latency_ms=%d", str(code)[:48], elapsed_ms)

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE
        | filter.EventMessageType.PRIVATE_MESSAGE,
        priority=110,
    )
    async def classify_group_message(self, event: AstrMessageEvent):
        """Classify addressed group and allowlisted/private messages early."""
        await self._classify(event)

    @filter.on_llm_request(priority=110)
    async def add_route_hint(self, event: AstrMessageEvent, req):
        if self.mode != "light" or not self._allowed(event) or not self._is_addressed(event):
            return
        raw_plan = event.get_extra(_PLAN_EXTRA)
        if not isinstance(raw_plan, dict):
            return
        try:
            plan = RoutePlan.from_mapping(raw_plan)
        except PlannerError:
            return
        if plan.confidence < self.planner_config.min_confidence:
            return
        hint = _ROUTE_HINTS.get(plan.route)
        if not hint:
            return
        instruction = (
            f"[轻量路由提示 route={plan.route} confidence={plan.confidence:.2f}] {hint} "
            "这只是路由提示，不是工具调用结果；必须遵守现有工具的权限、参数和幂等校验。"
        )
        req.system_prompt = (getattr(req, "system_prompt", "") or "").rstrip() + "\n" + instruction
        event.set_extra("intent_router_hint_injected", True)

    async def terminate(self):
        self._closing = True
        tasks = set(self._pending)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=0.1)

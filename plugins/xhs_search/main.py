"""AstrBot integration for a private, read-only Xiaohongshu search service.

The plugin deliberately does not accept a Xiaohongshu token from QQ.  The
local service is logged in once with ``/xhs login`` (a QR code); its cookies
stay on the server.  The plugin only reads a separately provisioned local API
token through :class:`XHSClient`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.core.star.filter.command import GreedyStr

from .core import XHSClient, XHSError


DEFAULT_ENDPOINT = "http://127.0.0.1:18060"
DEFAULT_API_TOKEN_FILE = "/etc/xhs/api-token"
MAX_QUERY_CHARS = 500
MAX_RESULTS = 10
TOOL_NAME = "xhs_search"
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "要搜索的小红书关键词"},
    },
    "required": ["query"],
    "additionalProperties": False,
}
TOOL_DESCRIPTION = (
    "搜索小红书公开内容并返回标题、链接和摘要。只用于当前用户明确要求的只读搜索；"
    "不要发布、评论、点赞、收藏或读取私密内容。query 只填写用户当前消息中的搜索词。"
)
ROUTING_INSTRUCTION = (
    "[小红书搜索能力：xhs_search(query) 已可用。仅在当前用户要求搜索小红书时调用，"
    "把当前用户的搜索词作为 query；此工具是只读的，不执行发布、评论、点赞、收藏或登录操作。"
    "没有搜索意图时不要调用。]"
)


def _text(event: AstrMessageEvent) -> str:
    # QQ Reply exposes .text too. Only the current user's top-level Plain
    # components may become an instruction; quoted text remains data.
    return " ".join(part.text for part in event.get_messages() if isinstance(part, Plain)).strip()


def _is_private_qq(event: AstrMessageEvent) -> bool:
    """Return true only when AstrBot positively identifies a QQ private chat."""
    try:
        if event.get_platform_name() != "qq_official":
            return False
    except Exception:
        return False
    marker = getattr(event, "is_private_chat", None)
    if callable(marker):
        try:
            marker = marker()
        except Exception:
            return False
    if marker is not None:
        return marker is True
    marker = getattr(event, "is_private", None)
    if marker is not None:
        return marker is True
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            if str(getter() or "").strip():
                return False
        except Exception:
            return False
    umo = str(getattr(event, "unified_msg_origin", "") or "").lower()
    return any(part in umo for part in (":friendmessage:", ":privatemessage:", ":directmessage:"))


def _safe_query(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    value = re.sub(r"^[：:，,\s]+|[。！？!?]+$", "", value).strip()
    return value[:MAX_QUERY_CHARS]


def _eligible_text(text: str) -> bool:
    # A quote or a command is not authority to use a search capability.
    return bool(text and not text.startswith(("/", ">", "```", "“", "‘", '"', "'", "「", "『")))


@star.register(
    name="xhs_search",
    author="local",
    desc="受限 QQ 会话中的小红书只读搜索（扫码登录）",
    version="2.0.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        cfg = dict(config or {})
        self.config = cfg
        self.allowed_umos = {
            str(value).strip() for value in (cfg.get("allowed_umos") or []) if str(value).strip()
        }
        self.allowed_senders = {
            str(value).strip() for value in (cfg.get("allowed_senders") or []) if str(value).strip()
        }
        self.login_umos = {
            str(value).strip() for value in (cfg.get("login_umos") or []) if str(value).strip()
        }
        self.login_senders = {
            str(value).strip() for value in (cfg.get("login_senders") or []) if str(value).strip()
        }
        self.endpoint = str(cfg.get("endpoint") or DEFAULT_ENDPOINT).strip()
        self.api_token_file = str(cfg.get("api_token_file") or DEFAULT_API_TOKEN_FILE).strip()
        try:
            self.timeout = max(5.0, min(float(cfg.get("timeout_seconds", 90)), 180.0))
        except (TypeError, ValueError):
            self.timeout = 90.0
        try:
            self.max_results = max(1, min(int(cfg.get("max_results", 5)), MAX_RESULTS))
        except (TypeError, ValueError):
            self.max_results = 5
        # QR/login and search requests are serialized. This prevents a busy
        # group from creating many upstream sessions and keeps QR state sane.
        self._upstream_lock = asyncio.Semaphore(1)

    def _event_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        return (
            str(getattr(event, "unified_msg_origin", "") or "").strip(),
            str(event.get_sender_id() or "").strip(),
        )

    def _allowed(self, event: AstrMessageEvent) -> bool:
        try:
            platform = event.get_platform_name()
        except Exception:
            return False
        if platform != "qq_official":
            return False
        umo, sender = self._event_identity(event)
        return bool(umo and sender and umo in self.allowed_umos and
                    (not self.allowed_senders or sender in self.allowed_senders))

    def _login_allowed(self, event: AstrMessageEvent) -> bool:
        if not _is_private_qq(event):
            return False
        umo, sender = self._event_identity(event)
        return bool(umo and sender and umo in self.login_umos and sender in self.login_senders)

    def _client(self) -> XHSClient:
        return XHSClient(endpoint=self.endpoint, token_file=self.api_token_file, timeout=self.timeout)

    @staticmethod
    def _claim(event: AstrMessageEvent) -> None:
        event.stop_event()
        setter = getattr(event, "should_call_llm", None)
        if callable(setter):
            setter(False)

    async def _send(self, event: AstrMessageEvent, text: str) -> None:
        await event.send(MessageChain([Plain(str(text)[:6000])]))

    async def _send_qr(self, event: AstrMessageEvent, qr: bytes) -> None:
        if not isinstance(qr, (bytes, bytearray)) or not qr:
            await self._send(event, "登录服务没有返回二维码，请稍后重试。")
            return
        try:
            chain = MessageChain([
                Plain("请使用小红书 App 扫描二维码登录；登录态会保存在服务器，不需要发送 token。"),
                Image.fromBytes(bytes(qr)),
            ])
            await event.send(chain)
            # The patched QQ adapter attaches receipts to this exact chain.
            # Older adapters do not expose them, so absence is not failure.
            receipts = getattr(chain, "qqofficial_sent_message_ids", None)
            if hasattr(chain, "qqofficial_sent_message_ids") and not receipts:
                await self._send(event, "二维码未确认送达，请稍后重新发送 /xhs login。")
        except Exception as error:
            self.logger.warning("XHS QR delivery failed type=%s", type(error).__name__)
            await self._send(event, "二维码发送失败，请稍后重试。")

    async def _call(self, method: str, *args, **kwargs):
        try:
            await asyncio.wait_for(self._upstream_lock.acquire(), timeout=0.5)
        except asyncio.TimeoutError as error:
            raise XHSError("小红书服务正在处理请求，请稍后再试。", code="busy") from error

        def invoke():
            return getattr(self._client(), method)(*args, **kwargs)

        task = asyncio.create_task(asyncio.to_thread(invoke))

        def completed(finished):
            # Cancellation of the QQ handler cannot cancel a running HTTP
            # thread. Retain the slot until the thread actually finishes.
            self._upstream_lock.release()
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    @staticmethod
    def _error_message(error: XHSError) -> str:
        code = str(getattr(error, "code", "") or "")
        if code in {"not_logged_in", "login_required"}:
            return "小红书尚未登录，请在 QQ 私聊执行 /xhs login 扫码。"
        if code == "already_logged_in":
            return "小红书已经登录，无需再次扫码。"
        if code == "busy":
            return "小红书服务正在处理请求，请稍后再试。"
        if code in {"invalid_query", "query_empty"}:
            return "请提供要搜索的小红书关键词。"
        # XHSError is expected to be safe, but keeping the mapping closed also
        # prevents a future provider exception from reaching QQ verbatim.
        return "小红书服务暂时不可用。"

    @filter.command("xhs", priority=100)
    async def xhs(self, event: AstrMessageEvent, body: GreedyStr):
        """Handle deterministic /xhs commands before the general LLM."""
        self._claim(event)
        raw = str(body or "").strip()
        if not raw:
            if self._allowed(event) or self._login_allowed(event):
                await self._send(event, "用法：/xhs search <关键词>｜/xhs login｜/xhs status｜/xhs logout")
            return
        command, _, argument = raw.partition(" ")
        command = command.lower().strip()
        argument = argument.strip()

        if command == "auth":
            if self._login_allowed(event):
                await self._send(event, "请使用 /xhs login 扫码，不要发送真实 token。插件不保存凭据，但 QQ 和框架可能已记录原消息。")
            return
        if command == "login":
            if not self._login_allowed(event):
                return
            try:
                qr = await self._call("login_qr")
            except XHSError as error:
                await self._send(event, self._error_message(error))
            except Exception as error:
                self.logger.warning("XHS login QR failed type=%s", type(error).__name__)
                await self._send(event, "登录服务暂时不可用，请稍后重试。")
            else:
                await self._send_qr(event, qr)
            return
        if command == "status":
            if not self._login_allowed(event):
                return
            try:
                logged_in = bool(await self._call("login_status"))
            except XHSError as error:
                await self._send(event, self._error_message(error))
            except Exception as error:
                self.logger.warning("XHS login status failed type=%s", type(error).__name__)
                await self._send(event, "登录服务暂时不可用，请稍后重试。")
            else:
                await self._send(event, "小红书已登录。" if logged_in else "小红书尚未登录，请执行 /xhs login 扫码。")
            return
        if command == "logout":
            if not self._login_allowed(event):
                return
            try:
                await self._call("logout")
            except XHSError as error:
                await self._send(event, self._error_message(error))
            except Exception as error:
                self.logger.warning("XHS logout failed type=%s", type(error).__name__)
                await self._send(event, "退出登录失败，请稍后重试。")
            else:
                await self._send(event, "小红书登录态已退出。")
            return
        if command != "search" or not self._allowed(event):
            return
        await self._search_and_reply(event, argument)

    async def _search(self, query: str) -> list[dict[str, Any]]:
        query = _safe_query(query)
        if not query:
            raise XHSError("请提供要搜索的小红书关键词。")
        result = await self._call("search", query, limit=self.max_results)
        return result if isinstance(result, list) else []

    async def _search_and_reply(self, event: AstrMessageEvent, query: str) -> None:
        query = _safe_query(query)
        if not query:
            await self._send(event, "用法：/xhs search <关键词>（最多 500 字符）")
            return
        try:
            results = await self._search(query)
        except XHSError as error:
            await self._send(event, self._error_message(error))
            return
        except Exception as error:
            self.logger.warning("XHS search failed type=%s", type(error).__name__)
            await self._send(event, "搜索服务暂时不可用，请稍后重试。")
            return
        await self._send(event, self._format_results(query, results))

    @staticmethod
    def _format_results(query: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return f"没有找到与“{query[:100]}”相关的小红书结果。"
        lines = [f"小红书搜索：{query[:100]}"]
        for index, item in enumerate(results, 1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "无标题").replace("\n", " ")[:300]
            lines.append(f"{index}. {title}")
            url = str(item.get("url") or "").strip()
            if url:
                lines.append(f"   {url[:1000]}")
            snippet = str(item.get("snippet") or "").replace("\n", " ").strip()
            if snippet:
                lines.append(f"   {snippet[:500]}")
        return "\n".join(lines)[:6000]

    @staticmethod
    def _explicit_query(text: str) -> str | None:
        if not _eligible_text(text) or re.search(r"不要|别|不用|不需要|不必|无须|无需|\b(?:don't|do not|never|without)\b", text, re.I):
            return None
        patterns = (
            r"(?:在|去|帮我在)?\s*(?:小红书|xhs|rednote)\s*(?:上|里)?\s*(?:搜索|搜|查找|查|找|看看)\s*(.+)$",
            r"(?:搜索|搜|查找|查|找)\s*(?:一下)?\s*(?:小红书|xhs|rednote)\s*(?:上|里)?\s*(.+)$",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                query = _safe_query(match.group(1))
                if query:
                    return query
        return None

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=105)
    async def natural_search(self, event: AstrMessageEvent):
        if not self._allowed(event) or not bool(getattr(event, "is_at_or_wake_command", False)):
            return
        text = _text(event)
        if not _eligible_text(text):
            return
        query = None
        if bool(event.get_extra("intent_router_accepted")):
            if event.get_extra("intent_router_route") != "xhs_search":
                return
            query = _safe_query(event.get_extra("intent_router_task") or "")
        if not query:
            query = self._explicit_query(text)
        if not query:
            return
        self._claim(event)
        await self._search_and_reply(event, query)

    def _semantic_authorized(self, event: AstrMessageEvent) -> bool:
        text = _text(event)
        binding = event.get_extra("xhs_search_semantic_authorized")
        expected = {"event": id(event), "owner": self._event_identity(event),
                    "text": hashlib.sha256(text.encode("utf-8")).hexdigest()}
        return (self._allowed(event) and bool(getattr(event, "is_at_or_wake_command", False)) and
                _eligible_text(text) and binding == expected)

    @filter.on_llm_request(priority=105)
    async def provide_search_tool(self, event: AstrMessageEvent, req):
        if not self._allowed(event) or not bool(getattr(event, "is_at_or_wake_command", False)):
            return
        text = _text(event)
        if not _eligible_text(text):
            return
        try:
            from astrbot.core.agent.tool import FunctionTool, ToolSet
            req.func_tool = ToolSet(list(req.func_tool.tools) if req.func_tool else [])
            req.func_tool.add_tool(FunctionTool(
                name=TOOL_NAME, description=TOOL_DESCRIPTION, parameters=TOOL_SCHEMA,
                handler=self.semantic_search,
            ))
        except Exception as error:
            self.logger.warning("XHS tool injection failed type=%s", type(error).__name__)
            return
        setter = getattr(event, "set_extra", None)
        if callable(setter):
            setter("xhs_search_semantic_authorized", {
                "event": id(event), "owner": self._event_identity(event),
                "text": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            })
            if not event.get_extra("xhs_search_semantic_instructions"):
                req.system_prompt = (getattr(req, "system_prompt", "") or "").rstrip() + "\n" + ROUTING_INSTRUCTION
                setter("xhs_search_semantic_instructions", True)

    async def semantic_search(self, event: AstrMessageEvent, query: str = "") -> str:
        if not self._semantic_authorized(event):
            return json.dumps({"ok": False, "error": "当前消息未授权使用小红书搜索。"}, ensure_ascii=False)
        query = _safe_query(query) or _safe_query(event.get_extra("intent_router_task") or "")
        if not query:
            return json.dumps({"ok": False, "error": "请提供搜索关键词。"}, ensure_ascii=False)
        try:
            results = await self._search(query)
        except XHSError as error:
            return json.dumps({"ok": False, "error": self._error_message(error)}, ensure_ascii=False)
        except Exception as error:
            self.logger.warning("XHS semantic search failed type=%s", type(error).__name__)
            return json.dumps({"ok": False, "error": "搜索服务暂时不可用。"}, ensure_ascii=False)
        return json.dumps({"ok": True, "results": results}, ensure_ascii=False)


__all__ = ["Main", "TOOL_NAME", "TOOL_SCHEMA"]

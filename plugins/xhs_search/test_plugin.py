from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parent
PACKAGE = "xhs_plugin_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package


def _decorator(*_args, **_kwargs):
    return lambda fn: fn


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _Image:
    def __init__(self, data=b""):
        self.data = data

    @staticmethod
    def fromBytes(data):
        return _Image(data)


class _Chain(list):
    pass


class _Reply:
    def __init__(self, message_str=""):
        self.message_str = message_str


star = types.SimpleNamespace(
    Star=type("Star", (), {"__init__": lambda self, _context: setattr(self, "logger", logging.getLogger("xhs-tests"))}),
    Context=object,
    register=_decorator,
)
filters = types.SimpleNamespace(
    command=_decorator,
    event_message_type=_decorator,
    on_llm_request=_decorator,
    on_decorating_result=_decorator,
    EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1, PRIVATE_MESSAGE=2),
)
modules = {
    "astrbot": types.ModuleType("astrbot"),
    "astrbot.api": types.ModuleType("astrbot.api"),
    "astrbot.api.event": types.ModuleType("astrbot.api.event"),
    "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
    "astrbot.core": types.ModuleType("astrbot.core"),
    "astrbot.core.star": types.ModuleType("astrbot.core.star"),
    "astrbot.core.star.filter": types.ModuleType("astrbot.core.star.filter"),
    "astrbot.core.star.filter.command": types.ModuleType("astrbot.core.star.filter.command"),
}
modules["astrbot.api"].star = star
modules["astrbot.api.event"].AstrMessageEvent = object
modules["astrbot.api.event"].MessageChain = _Chain
modules["astrbot.api.event"].filter = filters
modules["astrbot.api.message_components"].Plain = _Plain
modules["astrbot.api.message_components"].Image = _Image
modules["astrbot.api.message_components"].Reply = _Reply
modules["astrbot.core.star.filter.command"].GreedyStr = str
sys.modules.update(modules)


core_spec = importlib.util.spec_from_file_location(PACKAGE + ".core", ROOT / "core.py")
core = importlib.util.module_from_spec(core_spec)
sys.modules[core_spec.name] = core
assert core_spec.loader is not None
core_spec.loader.exec_module(core)
spec = importlib.util.spec_from_file_location(PACKAGE + ".main", ROOT / "main.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
assert spec.loader is not None
spec.loader.exec_module(plugin)


class Event:
    def __init__(self, text, *, sender="owner", umo="qq_official:GroupMessage:g", private=False):
        self.parts = [_Plain(text)]
        self.unified_msg_origin = umo
        self.sender = sender
        self.is_private_chat = private
        self.is_at_or_wake_command = True
        self.sent = []
        self.stopped = False
        self.extras = {}

    def get_platform_name(self):
        return "qq_official"

    def get_sender_id(self):
        return self.sender

    def get_messages(self):
        return self.parts

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, _value):
        pass

    async def send(self, chain):
        if hasattr(self, "receipt_ids"):
            chain.qqofficial_sent_message_ids = list(self.receipt_ids)
        self.sent.append(chain)


class ToolSet:
    def __init__(self, tools):
        self.tools = tools

    def add_tool(self, tool):
        self.tools = [item for item in self.tools if item.name != tool.name] + [tool]


class Tests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        return plugin.Main(None, {
            "allowed_umos": ["qq_official:GroupMessage:g"],
            "login_umos": ["qq_official:FriendMessage:owner"],
            "login_senders": ["owner"],
            "endpoint": "http://127.0.0.1:18060",
        })

    @staticmethod
    def output(event):
        return "\n".join("".join(getattr(part, "text", "") for part in chain) for chain in event.sent)

    async def install_tool(self, p, event, tools=None):
        req = types.SimpleNamespace(func_tool=tools, system_prompt="original")
        fake = types.ModuleType("astrbot.core.agent.tool")
        fake.ToolSet = ToolSet
        fake.FunctionTool = lambda **kwargs: types.SimpleNamespace(**kwargs)
        with patch.dict(sys.modules, {fake.__name__: fake}):
            await p.provide_search_tool(event, req)
        return req

    async def test_login_is_private_owner_only_and_has_qr(self):
        p = self.make()
        group = Event("login")
        await p.xhs(group, "login")
        self.assertEqual(group.sent, [])
        owner = Event("login", umo="qq_official:FriendMessage:owner", private=True)
        with patch.object(p, "_call", new=AsyncMock(return_value=b"png")) as call:
            await p.xhs(owner, "login")
        call.assert_awaited_once_with("login_qr")
        self.assertEqual(owner.sent[0][1].data, b"png")

    async def test_search_allowlist_and_no_token_argument(self):
        p = self.make()
        denied = Event("search cats", umo="qq_official:GroupMessage:other")
        await p.xhs(denied, "search cats")
        self.assertEqual(denied.sent, [])
        allowed = Event("search cats")
        with patch.object(p, "_call", new=AsyncMock(return_value={"results": [{"title": "Cat", "url": "https://x"}], "references": {}})) as call:
            await p.xhs(allowed, "search cats")
        call.assert_awaited_once_with("search_bundle", "cats", limit=5)
        self.assertIn("Cat", "".join(getattr(x, "text", "") for x in allowed.sent[0]))

    async def test_natural_search_uses_light_task_without_regex(self):
        p = self.make()
        event = Event("帮我找一些适合通勤的内容")
        event.set_extra("intent_router_accepted", True)
        event.set_extra("intent_router_route", "xhs_search")
        event.set_extra("intent_router_task", "适合通勤的防晒")
        with patch.object(p, "_call", new=AsyncMock(return_value={"results": [], "references": {}})) as call:
            await p.natural_search(event)
        call.assert_awaited_once_with("search_bundle", "适合通勤的防晒", limit=5)
        self.assertTrue(event.stopped)

    async def test_auth_is_rejected_without_storage(self):
        p = self.make()
        owner = Event("auth secret-token", umo="qq_official:FriendMessage:owner", private=True)
        await p.xhs(owner, "auth secret-token")
        self.assertIn("扫码", "".join(getattr(x, "text", "") for x in owner.sent[0]))
        self.assertNotIn("secret-token", self.output(owner))

    async def test_default_configuration_denies_all_commands(self):
        p = plugin.Main(None)
        with patch.object(p, "_call", new=AsyncMock()) as call:
            for body in ("login", "status", "logout", "auth secret", "search cats", ""):
                event = Event(body, umo="qq_official:FriendMessage:owner", private=True)
                await p.xhs(event, body)
                self.assertTrue(event.stopped)
                self.assertEqual(event.sent, [])
        call.assert_not_awaited()

    async def test_login_requires_both_exact_umo_and_exact_sender(self):
        p = self.make()
        with patch.object(p, "_call", new=AsyncMock()) as call:
            for sender, umo, private in (
                ("other", "qq_official:FriendMessage:owner", True),
                ("owner", "qq_official:FriendMessage:other", True),
                ("owner", "qq_official:FriendMessage:owner", False),
            ):
                for body in ("login", "status", "logout"):
                    event = Event(body, sender=sender, umo=umo, private=private)
                    await p.xhs(event, body)
                    self.assertEqual(event.sent, [])
        call.assert_not_awaited()

    async def test_search_permission_is_separate_from_login_permission(self):
        p = self.make()
        owner = Event("search cats", umo="qq_official:FriendMessage:owner", private=True)
        await p.xhs(owner, "search cats")
        self.assertEqual(owner.sent, [])
        p.allowed_senders = {"owner"}
        other = Event("search cats", sender="other")
        await p.xhs(other, "search cats")
        self.assertEqual(other.sent, [])

    async def test_service_failures_never_echo_or_log_exception_text(self):
        p = self.make()
        for failure in (core.XHSError("upstream secret detail"), RuntimeError("upstream secret detail")):
            event = Event("search cats")
            with patch.object(p, "_call", new=AsyncMock(side_effect=failure)), patch.object(p.logger, "warning") as warning:
                await p.xhs(event, "search cats")
            self.assertNotIn("secret", self.output(event))
            for logged in warning.call_args_list:
                self.assertNotIn("secret", str(logged))

    async def test_status_and_logout_use_private_service_operations(self):
        p = self.make()
        status = Event("status", umo="qq_official:FriendMessage:owner", private=True)
        with patch.object(p, "_call", new=AsyncMock(return_value={"is_logged_in": True, "login_pending": False})) as call:
            await p.xhs(status, "status")
        call.assert_awaited_once_with("login_details")
        self.assertIn("已登录", self.output(status))
        logout = Event("logout", umo="qq_official:FriendMessage:owner", private=True)
        with patch.object(p, "_call", new=AsyncMock(return_value=None)) as call:
            await p.xhs(logout, "logout")
        call.assert_awaited_once_with("logout")
        self.assertIn("已退出", self.output(logout))

    async def test_semantic_tool_is_event_scoped_and_no_token_parameters(self):
        p = self.make()
        event = Event("给我找点在小红书上分享的收纳经验")
        existing = ToolSet([types.SimpleNamespace(name="web_search")])
        req = await self.install_tool(p, event, existing)
        self.assertEqual([item.name for item in existing.tools], ["web_search"])
        tool = next(item for item in req.func_tool.tools if item.name == plugin.TOOL_NAME)
        self.assertEqual(set(tool.parameters["properties"]), {"query"})
        with patch.object(p, "_call", new=AsyncMock(return_value={"results": [], "references": {}})) as call:
            result = json.loads(await tool.handler(event, query="收纳经验"))
            self.assertTrue(result["ok"])
            other = Event(event.parts[0].text)
            other.extras = dict(event.extras)
            denied = json.loads(await tool.handler(other, query="other"))
            self.assertFalse(denied["ok"])
            event.sender = "changed"
            denied = json.loads(await tool.handler(event, query="other"))
            self.assertFalse(denied["ok"])
        call.assert_awaited_once_with("search_bundle", "收纳经验", limit=5)

    async def test_semantic_tool_requires_allowed_addressed_noncommand_event(self):
        p = self.make()
        for event in (Event("search", umo="denied"), Event("/xhs auth secret"), Event("> 帮我搜索小红书猫")):
            self.assertIsNone((await self.install_tool(p, event)).func_tool)
        event = Event("小红书上的露营攻略")
        event.is_at_or_wake_command = False
        self.assertIsNone((await self.install_tool(p, event)).func_tool)

    async def test_explicit_fallback_respects_negative_quote_and_other_light_route(self):
        p = self.make()
        with patch.object(p, "_call", new=AsyncMock()) as call:
            for text in ("不要在小红书搜索猫", "> 在小红书搜索猫", "“小红书搜索猫”"):
                event = Event(text)
                await p.natural_search(event)
                self.assertFalse(event.stopped)
            event = Event("小红书搜索猫")
            event.set_extra("intent_router_accepted", True)
            event.set_extra("intent_router_route", "chat")
            await p.natural_search(event)
            self.assertFalse(event.stopped)
        call.assert_not_awaited()

    async def test_upstream_concurrency_is_bounded_without_queueing(self):
        p = self.make()
        await p._upstream_lock.acquire()
        try:
            with self.assertRaises(core.XHSError) as raised:
                await p._call("search", "cats")
        finally:
            p._upstream_lock.release()
        self.assertEqual(raised.exception.code, "busy")

    async def test_qr_missing_receipt_reports_failure_only_with_adapter_support(self):
        p = self.make()
        supported = Event("login", umo="qq_official:FriendMessage:owner", private=True)
        supported.receipt_ids = []
        await p._send_qr(supported, b"synthetic-png")
        self.assertEqual(len(supported.sent), 2)
        self.assertIn("未确认送达", self.output(supported))
        delivered = Event("login", umo="qq_official:FriendMessage:owner", private=True)
        delivered.receipt_ids = ["synthetic-receipt"]
        await p._send_qr(delivered, b"synthetic-png")
        self.assertEqual(len(delivered.sent), 1)
        legacy = Event("login", umo="qq_official:FriendMessage:owner", private=True)
        await p._send_qr(legacy, b"synthetic-png")
        self.assertEqual(len(legacy.sent), 1)

    async def test_quoted_reply_text_cannot_supply_search_instruction(self):
        p = self.make()
        event = Event("这句话是什么意思")
        event.parts.append(types.SimpleNamespace(text="小红书搜索猫咪"))
        self.assertEqual(plugin._text(event), "这句话是什么意思")
        with patch.object(p, "_call", new=AsyncMock()) as call:
            await p.natural_search(event)
        call.assert_not_awaited()
        event.parts = [types.SimpleNamespace(text="小红书搜索猫咪")]
        self.assertEqual(plugin._text(event), "")
        self.assertIsNone((await self.install_tool(p, event)).func_tool)

    async def test_historical_status_requests_use_local_service_without_codex(self):
        p = self.make()
        for text in ("已登录，检查下小红书的登录态", "我已经扫码登录了，你检查下状态", "我已经扫码确认了"):
            event = Event(text, umo="qq_official:FriendMessage:owner", private=True)
            p._pending_login[p._event_identity(event)] = time.monotonic() + 240
            with patch.object(p, "_call", new=AsyncMock(return_value={"is_logged_in": True})) as call:
                await p.natural_login(event)
            call.assert_awaited_once_with("login_details")
            self.assertTrue(event.stopped)
            self.assertIn("已登录", self.output(event))
            self.assertNotIn("Computer Use", self.output(event))

    async def test_waiting_login_followup_does_not_replace_qr(self):
        p = self.make()
        event = Event("我已经扫码确认了", umo="qq_official:FriendMessage:owner", private=True)
        p._pending_login[p._event_identity(event)] = time.monotonic() + 240
        with patch.object(p, "_call", new=AsyncMock(return_value={"is_logged_in": False, "login_pending": True})) as call:
            await p.natural_login(event)
            followup = Event("再看下", umo=event.unified_msg_origin, private=True)
            await p.natural_login(followup)
        self.assertEqual(call.await_count, 2)
        self.assertIn("等待", self.output(followup))
        self.assertTrue(followup.stopped)

    async def test_authorized_private_owner_can_search_and_receive_status_tool(self):
        p = self.make()
        event = Event("小红书搜索南京剧本杀", umo="qq_official:FriendMessage:owner", private=True)
        p.allowed_umos.add(event.unified_msg_origin)
        event.is_at_or_wake_command = False
        with patch.object(p, "_call", new=AsyncMock(return_value={"results": [], "references": {}})) as call:
            await p.natural_search(event)
        call.assert_awaited_once_with("search_bundle", "南京剧本杀", limit=5)
        status = Event("小红书现在可以用吗", umo=event.unified_msg_origin, private=True)
        req = await self.install_tool(p, status)
        self.assertIn(plugin.STATUS_TOOL_NAME, [tool.name for tool in req.func_tool.tools])
        with patch.object(p, "_call", new=AsyncMock(return_value={"is_logged_in": False})):
            result = json.loads(await p.semantic_status(status))
        self.assertEqual(result["state"], "not_logged_in")
        copied = Event(status.parts[0].text, umo=status.unified_msg_origin, private=True)
        copied.extras = dict(status.extras)
        self.assertFalse(json.loads(await p.semantic_status(copied))["ok"])

    async def test_login_status_does_not_expand_account_permissions(self):
        p = self.make()
        denied = Event("已登录，检查下小红书的登录态", sender="other", umo="qq_official:FriendMessage:owner", private=True)
        with patch.object(p, "_call", new=AsyncMock()) as call:
            await p.natural_login(denied)
            group = Event("检查下小红书的登录态")
            await p.natural_login(group)
        call.assert_not_awaited()
        self.assertFalse(denied.stopped)
        self.assertIn("私聊", self.output(group))
        for text in ("不要检查小红书登录态", "例如检查小红书登录态", "如何实现小红书登录", "> 已扫码登录"):
            self.assertIsNone(plugin.login_action(text))

    async def test_status_error_does_not_claim_user_logged_out(self):
        p = self.make()
        event = Event("我已经扫码确认了", umo="qq_official:FriendMessage:owner", private=True)
        p._pending_login[p._event_identity(event)] = time.monotonic() + 240
        with patch.object(p, "_call", new=AsyncMock(side_effect=core.XHSError("unsafe", code="status_unavailable"))):
            await p.natural_login(event)
        self.assertIn("不代表", self.output(event))
        self.assertNotIn("尚未登录", self.output(event))

    async def test_other_service_login_is_not_claimed_and_status_permission_is_separate(self):
        p = self.make()
        for text in ("我已经登录了", "我已经扫码确认了", "微信已扫码登录", "飞书扫码完成了"):
            event = Event(text, umo="qq_official:FriendMessage:owner", private=True)
            with patch.object(p, "_call", new=AsyncMock()) as call:
                await p.natural_login(event)
            call.assert_not_awaited()
            self.assertFalse(event.stopped)
        self.assertIsNone(plugin.login_action("微信已经扫码确认了", pending=True))
        event = Event("检查小红书登录态", umo="qq_official:FriendMessage:owner", private=True)
        request = await self.install_tool(p, event)
        self.assertEqual([tool.name for tool in request.func_tool.tools], [plugin.STATUS_TOOL_NAME])
        self.assertFalse(json.loads(await p.semantic_search(event, "cats"))["ok"])

    async def test_search_refresh_returns_continuation_before_reading_note(self):
        instance = self.make()
        event = Event("总结小红书笔记，要带图")
        await self.install_tool(instance, event)
        bundle = {"results": [], "references": {"synthetic-note": {"feed_id": "synthetic-note", "xsec_token": "synthetic-access"}}}
        note = {"note_id": "synthetic-note", "title": "合成笔记", "body": "合成正文", "image_urls": []}
        with patch.object(instance, "_call", new=AsyncMock(side_effect=[bundle, note])) as call:
            first = json.loads(await instance.semantic_read_notes(event, query="桌游"))
            self.assertTrue(first["ok"])
            self.assertEqual(first["status"], "references_ready")
            self.assertEqual(first["notes"], [])
            self.assertEqual(first["image_count"], 0)
            self.assertNotIn("synthetic-access", json.dumps(first))
            call.assert_awaited_once_with("search_bundle", "桌游", limit=5)
            result = await instance.semantic_read_notes(event, **first["next_arguments"])
        document = json.loads(next(item.text for item in result.content if item.type == "text"))
        self.assertTrue(document["ok"])
        self.assertEqual(document["notes"][0]["body"], "合成正文")
        self.assertEqual(call.await_count, 2)

    async def test_note_detail_uses_remaining_budget_without_premature_cutoff(self):
        original_wait_for = asyncio.wait_for
        for elapsed, expected_timeout in ((34, 66), (0, 90), (90, 10), (91, None)):
            with self.subTest(elapsed=elapsed):
                instance = self.make()
                event = Event("总结小红书笔记，要带图")
                await self.install_tool(instance, event)
                instance._note_references.put(instance._event_identity(event), "桌游", {
                    "synthetic-note": {"feed_id": "synthetic-note", "xsec_token": "synthetic-access"},
                })
                note = {"note_id": "synthetic-note", "title": "合成笔记", "body": "合成正文", "image_urls": []}
                observed = []

                async def timed_wait(awaitable, *, timeout):
                    observed.append(timeout)
                    return await original_wait_for(awaitable, timeout=timeout)

                clock = types.SimpleNamespace(monotonic=unittest.mock.Mock(side_effect=[100, 100 + elapsed]))
                with patch.object(plugin, "time", clock), patch.object(plugin.asyncio, "wait_for", timed_wait), patch.object(
                    instance, "_call", new=AsyncMock(return_value=note)
                ) as call:
                    result = await instance.semantic_read_notes(event)
                document = json.loads(next(item.text for item in result.content if item.type == "text"))
                if expected_timeout is None:
                    call.assert_not_awaited()
                    self.assertFalse(document["ok"])
                    self.assertEqual(document["failures"][0]["code"], "time_budget")
                    self.assertEqual(observed, [])
                else:
                    self.assertTrue(document["ok"])
                    self.assertEqual(observed, [expected_timeout])
                    self.assertLessEqual(observed[0], 105 - elapsed - 5)

    async def test_planner_status_route_uses_account_permission(self):
        p = self.make()
        event = Event("确认一下账户是否就绪", umo="qq_official:FriendMessage:owner", private=True)
        event.set_extra("intent_router_accepted", True)
        event.set_extra("intent_router_route", "xhs_status")
        with patch.object(p, "_call", new=AsyncMock(return_value={"is_logged_in": True})) as call:
            await p.natural_login(event)
        call.assert_awaited_once_with("login_details")
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()

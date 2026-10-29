from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).parent
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


star = types.SimpleNamespace(
    Star=type("Star", (), {"__init__": lambda self, _context: setattr(self, "logger", logging.getLogger("xhs-tests"))}),
    Context=object,
    register=_decorator,
)
filters = types.SimpleNamespace(
    command=_decorator,
    event_message_type=_decorator,
    on_llm_request=_decorator,
    EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1),
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
        with patch.object(p, "_call", new=AsyncMock(return_value=[{"title": "Cat", "url": "https://x"}])) as call:
            await p.xhs(allowed, "search cats")
        call.assert_awaited_once_with("search", "cats", limit=5)
        self.assertIn("Cat", "".join(getattr(x, "text", "") for x in allowed.sent[0]))

    async def test_natural_search_uses_light_task_without_regex(self):
        p = self.make()
        event = Event("帮我找一些适合通勤的内容")
        event.set_extra("intent_router_accepted", True)
        event.set_extra("intent_router_route", "xhs_search")
        event.set_extra("intent_router_task", "适合通勤的防晒")
        with patch.object(p, "_call", new=AsyncMock(return_value=[])) as call:
            await p.natural_search(event)
        call.assert_awaited_once_with("search", "适合通勤的防晒", limit=5)
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
        with patch.object(p, "_call", new=AsyncMock(return_value=True)) as call:
            await p.xhs(status, "status")
        call.assert_awaited_once_with("login_status")
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
        with patch.object(p, "_call", new=AsyncMock(return_value=[])) as call:
            result = json.loads(await tool.handler(event, query="收纳经验"))
            self.assertTrue(result["ok"])
            other = Event(event.parts[0].text)
            other.extras = dict(event.extras)
            denied = json.loads(await tool.handler(other, query="other"))
            self.assertFalse(denied["ok"])
            event.sender = "changed"
            denied = json.loads(await tool.handler(event, query="other"))
            self.assertFalse(denied["ok"])
        call.assert_awaited_once_with("search", "收纳经验", limit=5)

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


if __name__ == "__main__":
    unittest.main()

import asyncio
import hashlib
import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1] / "plugins/codex_bridge"
PACKAGE = "_timeout_bridge_tests"


class Plain:
    def __init__(self, text):
        self.text = text


class Reply:
    pass


def decorator(*arguments, **keywords):
    return lambda handler: handler


def load():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(ROOT)]
    names = ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.event.filter", "astrbot.api.message_components",
             "astrbot.core", "astrbot.core.star", "astrbot.core.star.filter", "astrbot.core.star.filter.command")
    modules = {name: types.ModuleType(name) for name in names}
    modules[PACKAGE] = package
    modules["astrbot.api"].star = types.SimpleNamespace(
        Star=type("Star", (), {"__init__": lambda self, context: setattr(self, "logger", logging.getLogger("timeout-bridge-tests"))}), Context=object)
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = list
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        platform_adapter_type=decorator, command=decorator, event_message_type=decorator, on_llm_request=decorator,
        on_decorating_result=decorator, after_message_sent=decorator, EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1, PRIVATE_MESSAGE=2))
    modules["astrbot.api.event.filter"].PlatformAdapterType = types.SimpleNamespace(QQOFFICIAL=1)
    modules["astrbot.api.message_components"].Plain = Plain
    modules["astrbot.api.message_components"].Reply = Reply
    modules["astrbot.core.star.filter.command"].GreedyStr = str
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(PACKAGE + ".main", ROOT / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


plugin = load()


class Event:
    def __init__(self, text="请解释一下 Python 的闭包", sender="synthetic-owner", umo="qq:GroupMessage:synthetic"):
        self.parts = [Plain(text)]
        self.sender = sender
        self.unified_msg_origin = umo
        self.is_at_or_wake_command = True
        self.extras = {}
        self.stopped = False

    def get_messages(self):
        return self.parts

    def get_platform_name(self):
        return "qq_official"

    def get_sender_id(self):
        return self.sender

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def stop_event(self):
        self.stopped = True

    def should_call_llm(self, value):
        self.call_llm = value


def timeout(event):
    text = " ".join(part.text for part in event.parts if isinstance(part, Plain)).strip()
    event.set_extra("intent_router_timeout", {"event": id(event), "owner": plugin.owner_key(event.unified_msg_origin, event.sender),
                                              "text": hashlib.sha256(text.encode()).hexdigest()})


class TimeoutBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserved_memory_has_priority_over_timeout_and_semantic_delegation(self):
        bridge = self.make()
        source = Event('合成偏好。以上要加入长期记忆')
        timeout(source)
        source.set_extra('long_term_memory_request', {
            'event': id(source), 'owner': plugin.owner_key(source.unified_msg_origin, source.sender),
            'text': hashlib.sha256(source.parts[0].text.encode()).hexdigest(),
        })
        await bridge.natural_status(source)
        request = types.SimpleNamespace(func_tool=None, system_prompt='')
        await bridge.provide_delegation_tool(source, request)
        bridge._submit.assert_not_awaited()
        self.assertIsNone(request.func_tool)

    async def test_memory_plugin_guard_works_without_reservation_and_cannot_copy_binding(self):
        bridge = self.make()
        source = Event('合成偏好。以上要加入长期记忆')
        handler = Mock(return_value=True)
        bridge.context = types.SimpleNamespace(get_all_stars=lambda: [types.SimpleNamespace(
            name='long_term_memory', activated=True, star_cls=types.SimpleNamespace(handles_memory_request=handler))])
        timeout(source)
        await bridge.natural_status(source)
        bridge._submit.assert_not_awaited()
        handler.assert_called_once_with(source)
        bridge.context = None
        other = Event()
        other.set_extra('long_term_memory_request', {'event': id(source), 'owner': plugin.owner_key(source.unified_msg_origin, source.sender),
                                                  'text': hashlib.sha256(source.parts[0].text.encode()).hexdigest()})
        timeout(other)
        await bridge.natural_status(other)
        bridge._submit.assert_awaited_once_with(other, other.parts[0].text, 'read-only')

    def make(self):
        bridge = plugin.Main(None, {"allowed_umos": ["qq:GroupMessage:synthetic"], "allowed_senders": ["synthetic-owner"]})
        bridge._reply_parent = Mock(return_value=(None, []))
        bridge._submit = AsyncMock(return_value={"ok": True})
        bridge._show = AsyncMock()
        return bridge

    async def test_actual_timeout_submits_current_task_once_read_only(self):
        bridge = self.make()
        event = Event()
        timeout(event)
        await asyncio.gather(bridge.natural_status(event), bridge.natural_status(event))
        bridge._submit.assert_awaited_once_with(event, event.parts[0].text, "read-only")
        self.assertTrue(event.stopped)
        self.assertFalse(event.call_llm)
        request = types.SimpleNamespace(func_tool=None, system_prompt="")
        await bridge.provide_delegation_tool(event, request)
        self.assertIsNone(request.func_tool)

    async def test_without_timeout_ordinary_task_stays_local(self):
        bridge = self.make()
        await bridge.natural_status(Event())
        bridge._submit.assert_not_awaited()

    async def test_boolean_copied_and_modified_bindings_do_not_delegate(self):
        bridge = self.make()
        original = Event()
        timeout(original)
        for mode in ("boolean", "copy", "sender", "conversation", "text"):
            with self.subTest(mode=mode):
                event = Event()
                if mode == "boolean":
                    event.set_extra("intent_router_timeout", True)
                elif mode == "copy":
                    event.set_extra("intent_router_timeout", original.get_extra("intent_router_timeout"))
                else:
                    timeout(event)
                    if mode == "sender":
                        event.sender = "synthetic-other"
                    elif mode == "conversation":
                        event.unified_msg_origin = "qq:GroupMessage:other"
                    else:
                        event.parts = [Plain("请帮我翻译这句普通文字")]
                await bridge.natural_status(event)
        bridge._submit.assert_not_awaited()

    async def test_optout_examples_and_local_capabilities_do_not_delegate(self):
        bridge = self.make()
        texts = ("不要用 Codex，帮我解释一下 Python 的闭包", "例如帮我研究一下机器人框架", "> 请用 Codex 研究这个项目",
                 "十分钟后提醒我喝水", "检查小红书的登录态", "这张图片里有什么东西", "你好")
        for text in texts:
            with self.subTest(text=text):
                event = Event(text)
                timeout(event)
                await bridge.natural_status(event)
        bridge._submit.assert_not_awaited()

    async def test_travel_and_xhs_reservations_take_precedence(self):
        bridge = self.make()
        for name in ("travel_local_request", "xhs_notes_request"):
            event = Event("请给我整理刚才已经读取的资料")
            timeout(event)
            event.set_extra(name, True)
            await bridge.natural_status(event)
        bridge._submit.assert_not_awaited()

    async def test_disabled_automatic_routing_and_unaddressed_events_stay_local(self):
        for setting in ("auto_complex_enabled", "natural_language_enabled"):
            bridge = self.make()
            bridge.config[setting] = False
            event = Event()
            timeout(event)
            await bridge.natural_status(event)
            bridge._submit.assert_not_awaited()
        bridge = self.make()
        event = Event()
        timeout(event)
        event.is_at_or_wake_command = False
        await bridge.natural_status(event)
        bridge._submit.assert_not_awaited()

    async def test_timeout_cannot_enable_workspace_write(self):
        bridge = self.make()
        event = Event("请帮我以可写模式分析并修改这份文件")
        timeout(event)
        await bridge.natural_status(event)
        bridge._submit.assert_awaited_once_with(event, event.parts[0].text, "read-only")

    async def test_failure_is_not_resubmitted_by_duplicate_handler(self):
        bridge = self.make()
        bridge._submit.return_value = {"ok": False, "pending": True}
        event = Event()
        timeout(event)
        await bridge.natural_status(event)
        await bridge.natural_status(event)
        self.assertEqual(bridge._submit.await_count, 1)

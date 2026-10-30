"""Offline tests for the two-mode AstrBot wrapper."""
from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent
PACKAGE = "_intent_router_test_package"


def _decorator(*args, **kwargs):
    return lambda fn: fn


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeImage:
    pass


class FakeEvent:
    def __init__(self, text="请研究这个项目", *, addressed=True, sender="u1", umo="qq:GroupMessage:g", image=False):
        self.parts = [FakePlain(text)] + ([FakeImage()] if image else [])
        self.is_at_or_wake_command = addressed
        self.unified_msg_origin = umo
        self.sender = sender
        self.extras = {}

    def get_platform_name(self):
        return "qq_official"

    def get_messages(self):
        return self.parts

    def get_sender_id(self):
        return self.sender

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


class FakeResponse:
    def __init__(self, text):
        self.completion_text = text


class FakeProvider:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


def load_plugin():
    pkg = types.ModuleType(PACKAGE)
    pkg.__path__ = [str(ROOT)]
    star = types.SimpleNamespace(
        Star=type("Star", (), {"__init__": lambda self, context: setattr(self, "logger", types.SimpleNamespace(info=print, warning=print, error=print))}),
        Context=object,
        register=_decorator,
    )
    filters = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1, PRIVATE_MESSAGE=2),
        event_message_type=_decorator,
        on_llm_request=_decorator,
    )
    modules = {
        PACKAGE: pkg,
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.message_components": types.ModuleType("astrbot.api.message_components"),
    }
    modules["astrbot.api"].star = star
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = filters
    modules["astrbot.api.message_components"].Plain = FakePlain
    with patch.dict(sys.modules, modules):
        core_spec = importlib.util.spec_from_file_location(PACKAGE + ".core", ROOT / "core.py")
        core = importlib.util.module_from_spec(core_spec)
        sys.modules[PACKAGE + ".core"] = core
        core_spec.loader.exec_module(core)
        main_spec = importlib.util.spec_from_file_location(PACKAGE + ".main", ROOT / "main.py")
        main = importlib.util.module_from_spec(main_spec)
        sys.modules[PACKAGE + ".main"] = main
        main_spec.loader.exec_module(main)
    return main


plugin = load_plugin()


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_is_context_not_current_instruction(self):
        event = FakeEvent("这句话是什么意思")
        event.parts.insert(0, types.SimpleNamespace(text="去小红书搜索护肤品"))
        self.assertEqual(plugin._plain_text(event), "这句话是什么意思")

    async def test_regex_mode_never_calls_provider(self):
        provider = FakeProvider("{}")
        context = types.SimpleNamespace(get_provider_by_id=lambda _id: provider)
        instance = plugin.Main(context, {"mode": "regex", "allowed_umos": ["qq:GroupMessage:g"]})
        event = FakeEvent()
        await instance.classify_group_message(event)
        self.assertEqual(provider.calls, [])

    async def test_light_mode_stores_validated_plan_and_injects_static_hint(self):
        payload = {"route": "codex", "confidence": 0.96, "task": "请研究这个项目", "mode": "read-only", "missing_context": []}
        provider = FakeProvider(json.dumps(payload, ensure_ascii=False))
        requested_provider_ids = []
        def get_provider_by_id(provider_id):
            requested_provider_ids.append(provider_id)
            return provider
        context = types.SimpleNamespace(get_provider_by_id=get_provider_by_id)
        instance = plugin.Main(context, {"mode": "light", "allowed_umos": ["qq:GroupMessage:g"]})
        event = FakeEvent()
        await instance.classify_group_message(event)
        self.assertEqual(event.get_extra("intent_router_route"), "codex")
        req = types.SimpleNamespace(system_prompt="基础提示")
        await instance.add_route_hint(event, req)
        self.assertIn("route=codex", req.system_prompt)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(requested_provider_ids, ["planner-minimax-light"])
        self.assertEqual(provider.calls[0]["model"], "MiniMax-M3.1-Flash")

    async def test_light_failure_falls_back_without_plan(self):
        provider = FakeProvider("not json")
        context = types.SimpleNamespace(get_provider_by_id=lambda _id: provider)
        instance = plugin.Main(context, {"mode": "light", "allowed_umos": ["qq:GroupMessage:g"]})
        event = FakeEvent()
        await instance.classify_group_message(event)
        self.assertIsNone(event.get_extra("intent_router_plan"))
        self.assertIsNone(event.get_extra("intent_router_route"))

    async def test_denied_session_does_not_call_provider(self):
        provider = FakeProvider("{}")
        context = types.SimpleNamespace(get_provider_by_id=lambda _id: provider)
        instance = plugin.Main(context, {"mode": "light", "allowed_umos": []})
        await instance.classify_group_message(FakeEvent())
        self.assertEqual(provider.calls, [])



if __name__ == "__main__":
    unittest.main()

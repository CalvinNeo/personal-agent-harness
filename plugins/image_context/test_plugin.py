"""Offline handler integration tests with minimal framework interfaces; no sends."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeImage:
    def __init__(self, ref):
        self.url, self.file = ref, ref


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakePart:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self._no_save = False

    def mark_as_temp(self):
        self._no_save = True
        return self


class FakeImagePart(FakePart):
    ImageURL = FakePart


def _decorator(*args, **kwargs):
    return lambda fn: fn


def load_plugin():
    package_name = "_image_context_test_package"
    pkg = types.ModuleType(package_name)
    pkg.__path__ = [str(Path(__file__).parent)]
    star = types.SimpleNamespace(Star=type("Star", (), {"__init__": lambda self, context: None}),
                                 Context=object, register=_decorator)
    filters = types.SimpleNamespace(CustomFilter=object, EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1),
                                    event_message_type=_decorator, custom_filter=_decorator, on_llm_request=_decorator)
    modules = {package_name: pkg}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
                 "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message", "astrbot.core.provider",
                 "astrbot.core.provider.entities"):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api"].star = star
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = filters
    modules["astrbot.api.message_components"].Image = FakeImage
    modules["astrbot.api.message_components"].Plain = FakePlain
    modules["astrbot.core.agent.message"].ImageURLPart = FakeImagePart
    modules["astrbot.core.agent.message"].TextPart = FakePart
    modules["astrbot.core.provider.entities"].ProviderRequest = object
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(package_name + ".main", Path(__file__).parent / "main.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


plugin = load_plugin()


def ref(index):
    return f"https://multimedia.nt.qq.com/download?id={index}"


class FakeEvent:
    def __init__(self, text="", images=(), sender="alice", umo="qq:GroupMessage:g", addressed=True):
        self.unified_msg_origin = umo
        self.sender = sender
        self.is_at_or_wake_command = addressed
        self.parts = [FakePlain(text)] + [FakeImage(url) for url in images]
        self.extras = {}
        self.sent = []
        self.stopped = False

    def get_platform_name(self): return "qq_official"
    def get_sender_id(self): return self.sender
    def get_messages(self): return self.parts
    def get_extra(self, key): return self.extras.get(key)
    def set_extra(self, key, value): self.extras[key] = value
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return text
    async def send(self, result): self.sent.append(result)


def request():
    return types.SimpleNamespace(image_urls=[], extra_user_content_parts=[], contexts=[])


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = plugin.Main(None, {"allowed_umos": ["qq:GroupMessage:g"]})

    async def test_unmentioned_images_cached_silently_and_followup_injected(self):
        first = FakeEvent(images=[ref(1)], addressed=False)
        second = FakeEvent(images=[ref(2)], addressed=False)
        await self.plugin.remember_images(first)
        await self.plugin.remember_images(second)
        self.assertEqual(first.sent + second.sent, [])
        self.assertFalse(first.is_at_or_wake_command)
        later, req = FakeEvent("请总结刚才两张图"), request()
        await self.plugin.add_image_context(later, req)
        visual = [part for part in req.extra_user_content_parts if isinstance(part, FakeImagePart)]
        self.assertEqual([part.image_url.url for part in visual], [ref(1), ref(2)])
        self.assertTrue(all(part._no_save for part in req.extra_user_content_parts))
        self.assertEqual(req.image_urls, [])

    async def test_missing_and_other_sender_block_model(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("看一下上一张图", sender="bob"), request()
        await self.plugin.add_image_context(event, req)
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("重新发送", event.sent[0])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_allowlist_and_addressed_gates(self):
        for event in [FakeEvent("上一张图", umo="qq:GroupMessage:other"),
                      FakeEvent("上一张图", addressed=False)]:
            req = request()
            await self.plugin.add_image_context(event, req)
            self.assertEqual(event.sent, [])
            self.assertFalse(event.stopped)
            self.assertEqual(req.extra_user_content_parts, [])
        denied = plugin.Main(None, {})
        await denied.remember_images(FakeEvent(images=[ref(1)]))
        self.assertEqual(len(denied._cache._sessions), 0)

    async def test_current_attachment_not_duplicated(self):
        event, req = FakeEvent("看看刚才那张图", images=[ref(1)]), request()
        req.image_urls = ["/tmp/astrbot-current-image.png"]
        await self.plugin.remember_images(event)
        await self.plugin.add_image_context(event, req)
        self.assertFalse(any(isinstance(part, FakeImagePart) for part in req.extra_user_content_parts))
        self.assertEqual(req.image_urls, ["/tmp/astrbot-current-image.png"])

    async def test_hook_is_idempotent_and_history_copied(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("分析上一张图"), request()
        saved = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": ref(9)}}]}]
        req.contexts = saved
        await self.plugin.add_image_context(event, req)
        count = len(req.extra_user_content_parts)
        await self.plugin.add_image_context(event, req)
        self.assertEqual(len(req.extra_user_content_parts), count)
        self.assertEqual(saved[0]["content"][0]["type"], "image_url")
        self.assertEqual(req.contexts[0]["content"][0]["type"], "text")

    async def test_nested_images_and_text_urls_never_cached(self):
        event = FakeEvent("https://gchat.qpic.cn/a.jpg")
        event.parts += [types.SimpleNamespace(chain=[FakeImage(ref(1))])]
        self.assertFalse(plugin.HasTopLevelImage().filter(event, None))
        await self.plugin.remember_images(event)
        self.assertEqual(len(self.plugin._cache._sessions), 0)

    async def test_light_route_allows_unmatched_image_wording(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("看看这张", addressed=True), request()
        event.set_extra("intent_router_accepted", True)
        event.set_extra("intent_router_route", "image")
        await self.plugin.add_image_context(event, req)
        visual = [part for part in req.extra_user_content_parts if isinstance(part, FakeImagePart)]
        self.assertEqual([part.image_url.url for part in visual], [ref(1)])

    async def test_ordinary_llm_request_unchanged(self):
        event, req = FakeEvent("今天要做什么"), request()
        req.contexts = [{"role": "user", "content": "hello"}]
        original = req.contexts
        await self.plugin.add_image_context(event, req)
        self.assertIs(req.contexts, original)
        self.assertEqual(req.extra_user_content_parts, [])


if __name__ == "__main__":
    unittest.main()

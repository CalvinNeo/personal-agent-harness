"""Offline handler integration tests with minimal framework interfaces; no sends."""
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class FakeImage:
    def __init__(self, ref):
        self.url, self.file = ref, ref


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeReply:
    def __init__(self, chain=()):
        self.chain = chain


class FakeStar:
    def __init__(self, context):
        self.logger = Mock()


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
    star = types.SimpleNamespace(Star=FakeStar,
                                 Context=object, register=_decorator)
    filters = types.SimpleNamespace(CustomFilter=object, EventMessageType=types.SimpleNamespace(GROUP_MESSAGE=1, PRIVATE_MESSAGE=2),
                                    event_message_type=_decorator, custom_filter=_decorator, on_llm_request=_decorator)
    modules = {package_name: pkg}
    for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.message_components",
                 "astrbot.core", "astrbot.core.agent", "astrbot.core.agent.message", "astrbot.core.provider",
                 "astrbot.core.provider.entities", "astrbot.core.utils", "astrbot.core.utils.astrbot_path"):
        modules[name] = types.ModuleType(name)
    modules["astrbot.api"].star = star
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = filters
    modules["astrbot.api.message_components"].Image = FakeImage
    modules["astrbot.api.message_components"].Plain = FakePlain
    modules["astrbot.api.message_components"].Reply = FakeReply
    modules["astrbot.core.agent.message"].ImageURLPart = FakeImagePart
    modules["astrbot.core.agent.message"].TextPart = FakePart
    modules["astrbot.core.provider.entities"].ProviderRequest = object
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_temp_path = tempfile.gettempdir
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

    async def test_authorized_private_cache_and_followup_remain_separate_from_group(self):
        private = "qq:FriendMessage:alice"
        self.plugin.allowed_umos.add(private)
        await self.plugin.remember_images(FakeEvent(images=[ref(4)], umo=private))
        await self.plugin.remember_images(FakeEvent(images=[ref(9)]))
        source, req = FakeEvent("请总结刚才那张图", umo=private), request()
        await self.plugin.add_image_context(source, req)
        images = [part.image_url.url for part in req.extra_user_content_parts if isinstance(part, FakeImagePart)]
        self.assertEqual(images, [ref(4)])
        other = FakeEvent("上一张图", sender="bob", umo=private)
        denied = request()
        await self.plugin.add_image_context(other, denied)
        self.assertTrue(other.stopped)
        self.assertEqual(denied.extra_user_content_parts, [])

    async def test_missing_and_other_sender_block_model(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("看一下上一张图", sender="bob"), request()
        await self.plugin.add_image_context(event, req)
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("原始图片消息", event.sent[0])
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

    async def test_implicit_reference_works_without_planner(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("看看这张图", addressed=True), request()
        await self.plugin.add_image_context(event, req)
        visual = [part for part in req.extra_user_content_parts if isinstance(part, FakeImagePart)]
        self.assertEqual([part.image_url.url for part in visual], [ref(1)])

    async def test_planner_route_alone_cannot_attach_old_images(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(1)]))
        event, req = FakeEvent("今天吃什么", addressed=True), request()
        event.set_extra("intent_router_accepted", True)
        event.set_extra("intent_router_route", "image")
        await self.plugin.add_image_context(event, req)
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_ordinary_llm_request_unchanged(self):
        event, req = FakeEvent("今天要做什么"), request()
        req.contexts = [{"role": "user", "content": "hello"}]
        original = req.contexts
        await self.plugin.add_image_context(event, req)
        self.assertIs(req.contexts, original)
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_current_upload_wins_over_quote_in_both_routes(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(9)]))
        event = FakeEvent("分析这张图", images=[ref(1)])
        event.parts.append(FakeReply([FakeImage(ref(2))]))
        self.assertEqual(self.plugin.select_for_task(event), [ref(1)])
        req = request()
        req.image_urls = ["/tmp/current.png", "/tmp/old-quote.png"]
        await self.plugin.add_image_context(event, req)
        self.assertFalse(event.stopped)
        self.assertEqual(req.image_urls, [])
        self.assertEqual([p.image_url.url for p in req.extra_user_content_parts
                          if isinstance(p, FakeImagePart)], [ref(1)])

    async def test_current_count_cannot_be_filled_from_quote_or_cache(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(9)]))
        event = FakeEvent("分析这两张图", images=[ref(1)])
        event.parts.append(FakeReply([FakeImage(ref(2)), FakeImage(ref(3))]))
        with self.assertRaises(plugin.ImageReferenceError) as caught:
            self.plugin.select_for_task(event)
        self.assertEqual(caught.exception.code, "count_mismatch")
        req = request()
        await self.plugin.add_image_context(event, req)
        self.assertTrue(event.stopped)
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertIn("不足", event.sent[0])

    async def test_latest_failed_question_reports_missing_receipt(self):
        event = FakeEvent("根据我发的上一张图，判断我站在哪里")
        event.parts.append(FakeReply([FakePlain("根据我发的上一张图，判断我站在哪里")]))
        req = request()
        await self.plugin.add_image_context(event, req)
        self.assertTrue(event.stopped)
        self.assertIn("原始图片消息", event.sent[0])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_direct_original_reply_works_with_empty_cache(self):
        event = FakeEvent("根据这张图，判断我站在哪里")
        event.parts.append(FakeReply([FakeImage(ref(1))]))
        req = request()
        await self.plugin.add_image_context(event, req)
        self.assertFalse(event.stopped)
        self.assertEqual(self.plugin.select_for_task(event), [ref(1)])
        self.assertEqual([p.image_url.url for p in req.extra_user_content_parts
                          if isinstance(p, FakeImagePart)], [ref(1)])

    async def test_empty_qq_quote_never_uses_unrelated_cache_or_model(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(9)]))
        event = FakeEvent("看下上一张图")
        event.parts.append(FakeReply())
        with self.assertRaises(plugin.ImageReferenceError) as caught:
            self.plugin.select_for_task(event)
        self.assertEqual(caught.exception.code, "quote_missing")
        req = request()
        await self.plugin.add_image_context(event, req)
        self.assertTrue(event.stopped)
        self.assertIn("只传回了引用标识", event.sent[0])
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_empty_text_quote_or_framework_resolved_image_is_preserved(self):
        for text, prepared in [("谢谢", []), ("分析这张图", ["/tmp/resolved-quote.png"])]:
            event = FakeEvent(text)
            event.parts.append(FakeReply())
            req = request()
            req.image_urls = prepared.copy()
            await self.plugin.add_image_context(event, req)
            self.assertFalse(event.stopped)
            self.assertEqual(req.image_urls, prepared)

    async def test_short_quoted_picture_is_available_to_later_routes(self):
        await self.plugin.remember_images(FakeEvent(images=[ref(9)]))
        original = FakeEvent("看下")
        original.parts.append(FakeReply([FakeImage(ref(1))]))
        await self.plugin.add_image_context(original, request())
        later = FakeEvent("根据我发的上一张图，判断我站在哪里")
        self.assertEqual(self.plugin.select_for_task(later), [ref(1)])
        self.assertEqual(self.plugin.select_for_task(FakeEvent("分析刚才那几张图")), [ref(1)])
        req = request()
        await self.plugin.add_image_context(later, req)
        self.assertEqual([p.image_url.url for p in req.extra_user_content_parts
                          if isinstance(p, FakeImagePart)], [ref(1)])
        with self.assertRaises(plugin.ImageReferenceError):
            self.plugin.select_for_task(FakeEvent("上一张图", sender="bob"))
        with self.assertRaises(plugin.ImageReferenceError):
            self.plugin.select_for_task(FakeEvent("上一张图", umo="qq:GroupMessage:other"))

    async def test_task_quoted_image_is_available_to_next_request(self):
        event = FakeEvent("用 Codex 分析这张图")
        event.parts.append(FakeReply([FakeImage(ref(1))]))
        self.assertEqual(self.plugin.select_for_task(event), [ref(1)])
        self.assertEqual(self.plugin.select_for_task(FakeEvent("看看上一张图")), [ref(1)])


if __name__ == "__main__":
    unittest.main()

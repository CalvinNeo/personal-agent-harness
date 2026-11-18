"""Actual AstrBot API integration; synthetic attachments, no outbound sends."""
import asyncio
import json
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from unittest.mock import AsyncMock, patch

from PIL import Image as PILImage
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.agent.message import ImageURLPart, Message, dump_messages_with_checkpoints
from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.core.utils.image_input import prepare_request_images
from image_context.main import Main, HasTopLevelImage
from codex_bridge import media


def event(text="", refs=(), sender="alice", group="test-group", addressed=True, private=False):
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    msg.message = [Plain(text)] + [Image.fromURL(ref) for ref in refs]
    msg.message_str = text
    msg.sender = MessageMember(sender)
    msg.group_id = group
    msg.self_id = "bot"
    msg.message_id = "test-id"
    value = AstrMessageEvent(text, msg, PlatformMetadata("qq_official", "test", "test"), group)
    value.is_at_or_wake_command = addressed
    value.send = AsyncMock()
    return value


async def main():
    with tempfile.TemporaryDirectory() as folder:
        schema = json.loads((Path(__file__).resolve().parents[1] / "plugins/image_context/_conf_schema.json").read_text())
        cfg = AstrBotConfig(str(Path(folder) / "config.json"), schema=schema)
        assert cfg["allowed_umos"] == []
        plugin = Main(None, {"allowed_umos": [event().unified_msg_origin]})
        refs = [f"https://multimedia.nt.qq.com.cn/download?fileid=test{i}" for i in range(3)]
        first = event(refs=refs[:1], addressed=False)
        second = event(refs=refs[1:], addressed=False)
        await plugin.remember_images(first)
        await plugin.remember_images(second)
        assert not first.send.called and not second.send.called and not first.is_at_or_wake_command
        assert HasTopLevelImage().filter(first, {})
        assert not HasTopLevelImage().filter(event("ordinary"), {})
        follow = event("对比一下刚发的那几张图")
        old_history = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": refs[0]}}, {"type": "text", "text": "older"}]}]
        req = ProviderRequest(prompt=follow.message_str, contexts=old_history)
        await plugin.add_image_context(follow, req)
        parts = [p for p in req.extra_user_content_parts if isinstance(p, ImageURLPart)]
        assert [p.image_url.url for p in parts] == refs
        assert all(p._no_save for p in req.extra_user_content_parts)
        assert old_history[0]["content"][0]["type"] == "image_url"
        assert all(p["type"] != "image_url" for p in req.contexts[0]["content"])
        picture = Path(folder) / "square.png"
        PILImage.new("RGB", (48, 48), (200, 30, 40)).save(picture)
        async def mock_prepare(ref, **kwargs):
            return str(picture), False, False, "synthetic attachment"
        # Execute actual framework preparation and context serialization. Only
        # downloading is replaced by our local synthetic image.
        with patch("astrbot.core.utils.image_input.prepare_model_image", mock_prepare):
            await prepare_request_images(req, follow, max_size=2048, prepared={}, supports_image=True)
        assembled = await req.assemble_context()
        visuals = [p for p in assembled["content"] if p.get("type") == "image_url"]
        assert len(visuals) == 3 and all(p.get("_no_save") for p in visuals)
        assert all(p["image_url"]["url"].startswith("data:image/") for p in visuals)
        persisted = dump_messages_with_checkpoints([Message.model_validate(assembled)])
        assert not any(p.get("type") == "image_url" for p in persisted[0]["content"])
        for denied in [event("上一张图", sender="bob"), event("刚才四张图")]:
            denied_req = ProviderRequest(prompt=denied.message_str)
            await plugin.add_image_context(denied, denied_req)
            assert denied.is_stopped() and denied.send.call_count == 1
            assert not denied_req.extra_user_content_parts
        for ignored in [event("上一张图", group="other"), event("上一张图", addressed=False), event("你好")]:
            ignored_req = ProviderRequest(prompt=ignored.message_str)
            await plugin.add_image_context(ignored, ignored_req)
            assert not ignored.is_stopped() and not ignored.send.called
            assert not ignored_req.extra_user_content_parts
        await plugin.initialize()
        await plugin.terminate()
        assert not plugin._cache._sessions
        handlers = [h for h in star_handlers_registry if h.handler_module_path == "image_context.main"]
        assert len(handlers) == 3
        assert all(h.extras_configs["priority"] == 95 for h in handlers)
        private = event(refs=refs[:1], group="private-alice", private=True)
        plugin.allowed_umos.add(private.unified_msg_origin)
        remember = next(handler for handler in handlers if handler.handler_name == "remember_images")
        assert all(item.filter(private, {}) for item in remember.event_filters)
        await plugin.remember_images(private)
        follow = event("总结上一张图", group="private-alice", private=True)
        private_request = ProviderRequest(prompt=follow.message_str)
        await plugin.add_image_context(follow, private_request)
        assert [part.image_url.url for part in private_request.extra_user_content_parts if isinstance(part, ImageURLPart)] == refs[:1]
        assert not private.send.called and not follow.send.called
        # Reproduce both production failures using real framework components.
        missing = event("根据我发的上一张图，判断我站在哪里", group="receipt-group")
        plugin.allowed_umos.add(missing.unified_msg_origin)
        await plugin.add_image_context(missing, ProviderRequest(prompt=missing.message_str))
        assert missing.is_stopped() and "原始图片消息" in str(missing.send.call_args)
        quoted = event("看下上一张图", group="receipt-group")
        quoted.message_obj.message.append(Reply(id="synthetic-original-id", chain=[]))
        await plugin.add_image_context(quoted, ProviderRequest(prompt=quoted.message_str))
        assert quoted.is_stopped() and "只传回了引用标识" in str(quoted.send.call_args)
        direct = event("根据这张图，判断我站在哪里", refs=refs[:1], group="receipt-group")
        direct.message_obj.message.append(Reply(id="older", chain=[Image.fromURL(refs[1])]))
        direct_req = ProviderRequest(prompt=direct.message_str, image_urls=[refs[0], refs[1]])
        await plugin.add_image_context(direct, direct_req)
        assert not direct.is_stopped() and plugin.select_for_task(direct) == refs[:1]
        assert not direct_req.image_urls
        assert [part.image_url.url for part in direct_req.extra_user_content_parts if isinstance(part, ImageURLPart)] == refs[:1]
        insufficient = event("分析这两张图", refs=refs[:1], group="receipt-group")
        insufficient.message_obj.message.append(Reply(id="older", chain=[Image.fromURL(refs[1]), Image.fromURL(refs[2])]))
        await plugin.add_image_context(insufficient, ProviderRequest(prompt=insufficient.message_str))
        assert insufficient.is_stopped() and "不足" in str(insufficient.send.call_args)
        # A clean restart preserves a real received upload in its original TTL.
        saved = Path(folder) / "image-cache.json"
        persistent = Main(None, {"allowed_umos": [direct.unified_msg_origin], "cache_file": str(saved)})
        await persistent.remember_images(event(refs=refs[:1], group="receipt-group"))
        await persistent.terminate()
        restored = Main(None, {"allowed_umos": [direct.unified_msg_origin], "cache_file": str(saved)})
        await restored.initialize()
        assert restored.select_for_task(event("上一张图", group="receipt-group")) == refs[:1]
        assert saved.stat().st_mode & 0o777 == 0o600
        await restored.terminate()
        # A quoted attachment with ‘看下’ must seed the following request,
        # including after a restart, without merging an older upload batch.
        quoted_source = event("看下", group="receipt-group")
        quoted_source.message_obj.message.append(Reply(id="quoted-picture", chain=[Image.fromURL(refs[1])]))
        await persistent.add_image_context(quoted_source, ProviderRequest(prompt="看下"))
        assert persistent.select_for_task(event("刚才那几张图", group="receipt-group")) == refs[1:2]
        restarted = Main(None, {"allowed_umos": [direct.unified_msg_origin], "cache_file": str(saved)})
        await restarted.initialize()
        assert restarted.select_for_task(event("根据我发的上一张图，判断我站在哪里", group="receipt-group")) == refs[1:2]
        scoped_context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
            name="image_context", activated=True, star_cls=restarted)])
        # Run the real bridge selector and normalization, replacing only HTTP.
        with patch.object(media, "_download", new=AsyncMock(return_value=picture.read_bytes())) as download:
            images, quoted_text = await media.prepare_images(
                event("根据我发的上一张图，判断我站在哪里", group="receipt-group"), scoped_context)
        assert len(images) == 1 and not quoted_text
        download.assert_awaited_once_with(refs[1])
        await restarted.terminate()
        print("PASS image runtime: real imports/schema/registration/hooks, silent caching, isolation, 3-image preparation, temporary history, missing-image refusal, lifecycle")


asyncio.run(main())

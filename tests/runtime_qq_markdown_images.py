"""Real QQ SDK encoding, synthetic images, intercepted HTTP and COS uploads."""
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image as PillowImage
from botpy.api import BotAPI
from botpy.errors import ServerError
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent, MessageSesion
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata


def load_candidates():
    candidate = os.environ.get("QQ_MARKDOWN_ADAPTER_ROOT")
    if candidate:
        prefix = "astrbot.core.platform.sources.qqofficial."
        for name in ("markdown_images", "qqofficial_message_event", "qqofficial_platform_adapter"):
            spec = importlib.util.spec_from_file_location(prefix + name, Path(candidate) / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)


load_candidates()
from astrbot.core.platform.sources.qqofficial import markdown_images as gallery
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.platform.sources.qqofficial import qqofficial_platform_adapter as adapter


def image(color):
    output = io.BytesIO()
    PillowImage.new("RGB", (32, 24), color).save(output, format="PNG")
    return Image.fromBytes(output.getvalue())


class HTTP:
    def __init__(self, reject=False, fail_native=False):
        self.calls, self.messages, self.uploaded = [], [], 0
        self.reject, self.fail_native = reject, fail_native

    async def request(self, route, *, json=None, **unused):
        path = route.url
        self.calls.append((route.method, path))
        if path.endswith("/media-images/prepare"):
            self.uploaded += 1
            assert json["content_type"] == "image/png" and json["file_size"] < 2048
            return {"upload_id": str(self.uploaded), "presigned_url": "https://synthetic.cos.ap-guangzhou.myqcloud.com/image"}
        if path.endswith("/media-images/complete"):
            return {"image_id": "image_" + json["upload_id"], "url": "qqbot-media://image/v1/token_" + json["upload_id"]}
        if route.method == "DELETE":
            return {}
        if path.endswith("/files"):
            self.uploaded += 1
            return {"file_uuid": "native_" + str(self.uploaded), "file_info": str(self.uploaded), "ttl": 60}
        assert path.endswith("/v2/groups/synthetic-group/messages"), path
        self.messages.append(json)
        if self.reject and json.get("markdown"):
            raise ServerError("不允许发送原生 markdown")
        if self.fail_native and json.get("media", {}).get("file_info") == "4":
            raise ServerError("native second image rejected")
        return {"id": "receipt_" + str(len(self.messages))}


def event(http):
    raw = adapter.PatchedGroupMessage(None, "synthetic-event", {
        "id": "synthetic-source", "content": "合并展示图片", "timestamp": "2030-01-01T00:00:00+08:00",
        "group_openid": "synthetic-group", "author": {"member_openid": "synthetic-reader"},
        "mentions": [], "attachments": []})
    message = AstrBotMessage()
    message.type, message.message, message.message_str = MessageType.GROUP_MESSAGE, [Plain("合并展示图片")], "合并展示图片"
    message.sender = MessageMember("synthetic-reader")
    message.group_id, message.self_id, message.message_id = "synthetic-group", "synthetic-bot", "synthetic-source"
    message.raw_message = raw
    return QQOfficialMessageEvent(message.message_str, message, PlatformMetadata("qq_official", "test", "test"),
                                  "synthetic-group", SimpleNamespace(api=BotAPI(http)))


async def run():
    checks = []
    async def put(url, data, mime):
        return hashlib.md5(data).hexdigest()
    with patch.object(gallery, "put_image", side_effect=put), \
         patch.object(AstrMessageEvent, "send", new=AsyncMock()), \
         patch.object(adapter.Platform, "send_by_session", new=AsyncMock()):
        for delivery in ("event", "proactive"):
            for reject, fail_native in ((False, False), (True, False), (True, True)):
                http = HTTP(reject, fail_native)
                chain = MessageChain([Plain("第一张"), image("red"), Plain("第二张"), image("blue")], use_markdown_=True)
                if delivery == "event":
                    send = lambda: event(http).send(chain)
                else:
                    platform = object.__new__(adapter.QQOfficialPlatformAdapter)
                    platform.client, platform.use_markdown_default = SimpleNamespace(api=BotAPI(http)), False
                    platform._session_last_message_id = {"synthetic-group": "synthetic-source"}
                    platform._session_scene, platform._allow_group_proactive_send = {"synthetic-group": "group"}, True
                    session = MessageSesion("test", MessageType.GROUP_MESSAGE, "synthetic-group")
                    send = lambda: platform.send_by_session(session, chain)
                try:
                    await send()
                    assert not fail_native
                except ServerError:
                    assert fail_native
                if not reject:
                    assert len(http.messages) == 1
                    content = http.messages[0]["markdown"]["content"]
                    assert content.count("qqbot-media://image/v1/") == 2
                    assert "第一张" in content and "第二张" in content
                    assert "presigned" not in content
                    assert chain.qqofficial_delivered_image_indexes == [0, 1]
                else:
                    assert all(not call.get("content") or "qqbot-media://" not in call["content"] for call in http.messages)
                    assert chain.qqofficial_delivered_image_indexes == ([0] if fail_native else [0, 1])
                assert sum(method == "DELETE" for method, _ in http.calls) == 2
                checks.append({"path": delivery, "rejected_markdown": reject,
                               "partial_fallback": fail_native, "confirmed_images": len(chain.qqofficial_delivered_image_indexes)})
        original = MessageChain([image("red"), image("blue")], use_markdown_=False)
        http = HTTP()
        converted, ids = await gallery.prepare(http, original)
        assert converted is original and ids == [] and http.calls == []
        with patch.object(gallery, "put_image", side_effect=ValueError("synthetic upload failure")):
            original.use_markdown_ = True
            converted, ids = await gallery.prepare(http, original)
            assert converted is original and ids == []
    for url in ("http://bad.myqcloud.com/a", "https://evil.invalid/a", "https://127.0.0.1/a",
                "https://user:secret@safe.myqcloud.com/a", "https://safe.myqcloud.com:444/a"):
        try:
            await gallery.put_image(url, b"image", "image/png")
            raise AssertionError("unsafe upload accepted")
        except ValueError:
            pass
    report = {"ok": True, "checks": checks, "qq_messages_sent": 0, "external_network_calls": 0,
              "unsafe_upload_destinations_rejected": 5, "client_display_verified": False}
    Path(os.environ["QQ_MARKDOWN_REPORT"]).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(run())

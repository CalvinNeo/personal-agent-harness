"""Real framework, durable pixels, shared chat/Codex tools; no QQ or model calls."""
import asyncio
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from PIL import Image as PillowImage
from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.utils.image_input import prepare_request_images
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path
from image_context.main import Main
from codex_bridge import media
from codex_bridge.capabilities import CapabilityRegistry


def event(text="", *, sender="alice", group="room", private=False, mid="request", parts=()):
    msg = AstrBotMessage()
    msg.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    msg.message = [Plain(text), *parts]
    msg.message_str = text
    msg.sender = MessageMember(sender)
    msg.group_id = "" if private else group
    msg.self_id = "bot"
    msg.message_id = mid
    msg.timestamp = time.time() - 7 * 86400
    ev = AstrMessageEvent(text, msg, PlatformMetadata("qq_official", "test", "test"), group)
    ev.is_at_or_wake_command = True
    ev.send = AsyncMock()
    return ev


async def main():
    temp_root = Path(get_astrbot_temp_path()).resolve()
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        folder = Path(folder).resolve()
        picture = temp_root / "durable-synthetic.png"
        PillowImage.new("RGB", (160, 100), (240, 20, 40)).save(picture)
        original = picture.read_bytes()
        private = event(private=True, group="private-room")
        other = event(group="other-room")
        cfg = {"archive_dir": str(folder / "archive"), "allowed_umos": [
            event().unified_msg_origin, private.unified_msg_origin, other.unified_msg_origin]}
        plugin = Main(None, cfg)
        context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
            name="image_context", activated=True, star_cls=plugin)])
        # A seven-day-old receipt survives both its temp file and a restart.
        quoted = event("看下", mid="quoted-original", parts=[Reply(id="origin", chain=[Image.fromFileSystem(str(picture))])])
        await plugin.add_image_context(quoted, ProviderRequest(prompt="看下"))
        picture.unlink()
        plugin = Main(None, cfg)
        follow = event("能估算楼层么", mid="follow")
        refs = await plugin.prepare_for_task(follow)
        assert len(refs) == 1 and plugin.read_image_for_task(follow, refs[0]) == original
        req = ProviderRequest(prompt=follow.message_str)
        await plugin.add_image_context(follow, req)
        assert any(getattr(part, "image_url", None) for part in req.extra_user_content_parts)
        await prepare_request_images(req, follow, max_size=2048, prepared={}, supports_image=True)
        assembled = await req.assemble_context()
        assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/")
                   for p in assembled["content"])
        assert len((await media.prepare_images(event("能估算楼层么", mid="codex"), context))[0]) == 1
        id_only = event("看下这张图", mid="id-only", parts=[Reply(id="origin", chain=[])])
        assert await plugin.prepare_for_task(id_only) == refs
        assert len((await media.prepare_images(id_only, context))[0]) == 1
        text_quote = event("谢谢", mid="thanks", parts=[Reply(id="text-reply", chain=[])])
        await plugin.add_image_context(text_quote, ProviderRequest(prompt="谢谢"))
        assert not text_quote.is_stopped()
        # Group images belong to this group; private and other groups stay apart.
        assert await plugin.prepare_for_task(event(sender="bob", mid="other-member")) == refs
        assert await plugin.prepare_for_task(private) == []
        assert await plugin.prepare_for_task(other) == []
        picture.write_bytes(original)
        private_upload = event(private=True, group="private-room", mid="private-upload", parts=[Image.fromFileSystem(str(picture))])
        await plugin.remember_images(private_upload)
        assert len(await plugin.prepare_for_task(event("继续看", private=True, group="private-room", mid="private-follow"))) == 1
        assert await plugin.prepare_for_task(event("继续看", private=True, group="private-room", sender="bob", mid="denied-private")) == []
        # Both engines receive the exact same event-bound native image tools.
        registry = await CapabilityRegistry(context, follow).prepare()
        names = {s["function"]["name"] for s in registry.schemas()}
        assert {"list_current_chat_images", "read_current_chat_image"} <= names
        listed = await registry.execute("list_current_chat_images", {}, "list-1")
        assert listed["ok"] and len(listed["images"]) == 1
        image_id = listed["images"][0]["image_id"]
        result = await registry.execute("read_current_chat_image", {"image_id": image_id}, "read-1")
        blocks = result["_mcp_result"]["content"]
        assert any(block["type"] == "image" and base64.b64decode(block["data"]) for block in blocks)
        normal_tool = next(t for t in registry.function_tools() if t.name == "read_current_chat_image")
        normal = await normal_tool.handler(follow, image_id=image_id)
        assert any(b.type == "image" for b in normal.content)
        # Pagination cannot skip older pictures even with equal timestamps.
        for index in range(15):
            plugin._archive.ingest(*plugin._history_key(follow), "same-time-" + str(index),
                                   [(original, "synthetic")], received_wall_at=time.time() - 86400)
        page = plugin.list_history_for_task(follow, limit=12)
        assert page["has_more"] and page["next_cursor"]
        next_page = plugin.list_history_for_task(follow, cursor=page["next_cursor"])
        assert next_page["ok"] and len(page["images"]) + len(next_page["images"]) == 16
        picture.unlink(missing_ok=True)
        assert not quoted.send.called and not follow.send.called
        print("PASS durable images: original pixels survive seven days/temp deletion/restart; group/private boundaries; keywordless ordinary+Codex inputs; native history tools; complete pagination")


asyncio.run(main())

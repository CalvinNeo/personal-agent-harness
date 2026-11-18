"""Installed AstrBot integration: synthetic records, real media, no QQ network."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image as PillowImage
from astrbot.api.message_components import Image, Plain, Video
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.provider.entities import ProviderRequest

sys.path.insert(0, os.environ.get("ON_DEMAND_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "plugins")))
from codex_bridge.main import Main as Bridge
from codex_bridge.capabilities import CapabilityRegistry
from image_context.main import Main as Images
from long_term_memory.main import Main as Memory


def event(text="", *, sender="synthetic-owner", group="synthetic-group", private=False, mid="message"):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message = [Plain(text)]
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = "" if private else group
    message.self_id, message.message_id = "synthetic-bot", mid
    result = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), sender if private else group)
    result.is_at_or_wake_command = True
    result.send = AsyncMock(return_value=True)
    return result


async def main():
    destination = Path(os.environ["ON_DEMAND_REPORT"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory(prefix="on-demand-runtime-", dir=destination.parent) as directory:
        root = Path(directory)
        source = event("继续分析", mid="request")
        private = event(private=True)
        allowed = [source.unified_msg_origin, private.unified_msg_origin]
        conversation = SimpleNamespace(user_id=source.unified_msg_origin, cid="synthetic-history", history=json.dumps([
            {"role": "system", "content": "never expose synthetic system data"},
            {"role": "user", "content": "旧聊天：合成项目使用蓝色主题。"},
            {"role": "assistant", "content": "已确认合成主题。"},
        ]))
        context = SimpleNamespace(get_all_stars=lambda: [], send_message=AsyncMock(return_value=True),
            conversation_manager=SimpleNamespace(get_conversations=AsyncMock(return_value=[conversation])))
        memory = Memory(context, {"allowed_umos": allowed, "memory_file": str(root / "memory.sqlite3")})
        images = Images(context, {"allowed_umos": allowed, "archive_dir": str(root / "images")})
        bridge = Bridge(context, {"allowed_umos": allowed})
        bridge.routes_file = root / "jobs.json"
        bridge._monitor = AsyncMock()
        bridge._tool_monitor = AsyncMock()
        await memory.initialize()
        await bridge.initialize()
        context.get_all_stars = lambda: [SimpleNamespace(name="long_term_memory", activated=True, star_cls=memory),
                                        SimpleNamespace(name="image_context", activated=True, star_cls=images)]
        try:
            old = event("记住：合成测试喜欢蓝色杯子。", mid="memory-old")
            await memory.explicit_memory_request(old)
            stream = io.BytesIO()
            PillowImage.new("RGB", (100, 80), (255, 0, 0)).save(stream, "PNG")
            images._archive.ingest(*images._history_key(source), "old-image", [(stream.getvalue(), "synthetic")], received_wall_at=10)
            registry = await bridge._capability_registry(source)
            names = {item["function"]["name"] for item in registry.schemas()}
            required = {"list_current_chat_history", "search_current_chat_history", "read_current_chat_message",
                        "list_current_chat_images", "read_current_chat_image", "list_my_long_term_memories", "search_my_long_term_memories"}
            assert required <= names
            checks.append("keywordless request exposes all history, image and live-memory tools")

            rows = await registry.execute("search_current_chat_history", {"query": "蓝色主题"}, "history-1")
            assert rows["ok"] and rows["messages"] and "never expose" not in json.dumps(rows)
            message = await registry.execute("read_current_chat_message", {"message_id": rows["messages"][0]["message_id"]}, "history-read")
            assert "蓝色主题" in message["message"]["text"]
            incoming = event("新聊天：合成截止日期为周五。", sender="synthetic-member", mid="new-history")
            incoming.is_at_or_wake_command = False
            await bridge.archive_chat_message(incoming)
            latest = await registry.execute("search_current_chat_history", {"query": "周五"}, "history-2")
            assert latest["ok"] and len(latest["messages"]) == 1
            assert latest["messages"][0]["sender_id"] == "synthetic-member"
            checks.append("saved framework history and later unaddressed group receipts are queryable at call time")

            listed = await registry.execute("list_current_chat_images", {}, "image-list")
            picture = await registry.execute("read_current_chat_image", {"image_id": listed["images"][0]["image_id"]}, "image-read")
            assert any(part["type"] == "image" and base64.b64decode(part["data"]) for part in picture["_mcp_result"]["content"])
            checks.append("historical original pixels arrive as native MCP image content without an input attachment")

            first = await registry.execute("list_my_long_term_memories", {}, "memory-1")
            new = event("记住：合成测试晚饭后散步。", mid="memory-new")
            await memory.explicit_memory_request(new)
            second = await registry.execute("list_my_long_term_memories", {}, "memory-2")
            assert second["total"] == first["total"] + 1
            assert "散步" in json.dumps(second, ensure_ascii=False)
            checks.append("same active task reads newly stored memory without rebuilding a snapshot")

            ordinary = ProviderRequest(prompt="继续分析")
            await bridge.provide_current_chat_capabilities(source, ordinary)
            assert required <= {tool.name for tool in ordinary.func_tool.tools}
            context.conversation_manager.get_conversations.return_value = []
            isolated = await bridge._capability_registry(private)
            not_found = await isolated.execute("read_current_chat_message", {"message_id": rows["messages"][0]["message_id"]}, "denied")
            assert not_found["ok"] is False
            bridge.config["allowed_umos"] = []
            revoked = await registry.execute("list_current_chat_history", {}, "revoked")
            assert revoked.get("code") == "not_authorized"
            bridge.config["allowed_umos"] = allowed
            checks.append("ordinary and Codex share tools; group/private isolation and revoked allowlists are enforced")

            bridge._rpc = AsyncMock(return_value={"ok": True, "job": {"id": "1" * 16, "state": "queued"}})
            submitted = await bridge._submit(event("请用 Codex 继续分析", mid="submit"), "继续分析", notify=False)
            assert submitted["ok"]
            payload = bridge._rpc.call_args.args[0]
            assert not payload.get("images") and payload["memory_snapshot"] is None
            assert required <= {tool["name"] for tool in payload["capability_tools"]}
            checks.append("real bridge submits with no eagerly selected historical images or memory snapshot")

            clip = root / "clip.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10", "-t", "1",
                "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)],
                check=True, capture_output=True, timeout=30)
            data = clip.read_bytes()
            artifact = {"mime": "video/mp4", "name": "clip.mp4", "data": base64.b64encode(data).decode(),
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            bridge._rpc = AsyncMock(return_value={"ok": True, "artifact": artifact})
            chain = await bridge._artifact_chain({"id": "1" * 16}, "a" * 64, {"id": "0", "mime": "video/mp4"})
            try:
                assert isinstance(chain.chain[0], Video)
                encoded = await QQOfficialMessageEvent._parse_to_qqofficial(chain)
                assert str(Path(chain.chain[0].path)) in json.dumps(encoded)
                assert Path(chain.chain[0].path).read_bytes() == data
            finally:
                bridge._cleanup_artifact_chain(chain)
            assert not bridge._artifact_files
            checks.append("real H.264 MP4 becomes native QQ Video and its private temporary file is cleaned")
            context.send_message.assert_not_awaited()
        finally:
            await bridge.terminate()
            await memory.terminate()
        report = {"ok": True, "checks": checks, "count": len(checks), "qq_messages_sent": 0,
                  "real_framework": True, "real_video": True, "production_data_writes": 0,
                  "plugin_root": os.environ.get("ON_DEMAND_PLUGIN_ROOT")}
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))


asyncio.run(main())

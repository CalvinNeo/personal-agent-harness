from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Plain
from astrbot.core.pipeline.process_stage.method.star_request import StarRequestSubStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ["DIRECT_PLUGIN_ROOT"])
from codex_bridge.capabilities import CapabilityRegistry
from codex_bridge.history import ChatHistory
from codex_bridge.main import Main as Bridge
from codex_bridge.request_context import restore_event, snapshot_event
from long_term_memory.main import Main as Memory
from reminder.main import Main as Reminder
from travel_assistant.main import Main as Travel
from xhs_search.core import XHSClient
from xhs_search.main import Main as XHS


CHECKS = []
SERIAL = 0


def event(text, *, sender="synthetic-owner", group="synthetic-group"):
    global SERIAL
    SERIAL += 1
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message, message.message_str = [Plain(text)], text
    message.sender = MessageMember(sender)
    message.group_id = group
    message.self_id, message.message_id = "synthetic-bot", "synthetic-direct-" + str(SERIAL)
    result = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "direct", "direct"), group)
    result.is_at_or_wake_command = True
    result.send = AsyncMock(return_value=True)
    return result


def document(result):
    if isinstance(result, str):
        return json.loads(result)
    if "_mcp_result" in result:
        return json.loads(next(part["text"] for part in result["_mcp_result"]["content"] if part["type"] == "text"))
    return result


def passed(message):
    CHECKS.append(message)
    print("PASS " + message, flush=True)


def registered(instance, name):
    module = type(instance).__module__
    candidates = [handler for handler in star_handlers_registry._handlers
                  if handler.handler_module_path == module and handler.handler_name == name]
    assert len(candidates) == 1, (module, name, len(candidates))
    return replace(candidates[0], handler=getattr(instance, name))


async def main():
    destination = Path(os.environ["DIRECT_REPORT"])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-first-runtime-", dir=destination.parent) as directory:
        root = Path(directory)
        request = event("记住我喜欢简洁的回答")
        allowed = [request.unified_msg_origin]
        context = SimpleNamespace(send_message=AsyncMock(side_effect=AssertionError("QQ disabled")), get_all_stars=lambda: [])
        bridge = Bridge(context, {"allowed_umos": allowed, "codex_first_enabled": True})
        bridge.routes_file = root / "jobs.json"
        bridge._history_store = ChatHistory(root / "history.sqlite3")
        bridge._history_enabled = True
        memory = Memory(context, {"allowed_umos": allowed, "memory_file": str(root / "memory.sqlite3")})
        reminder = Reminder(context, {"allowed_umos": allowed, "reminder_file": str(root / "reminders.json")})
        travel = Travel(context, {"allowed_umos": allowed, "data_dir": str(root / "travel")})
        xhs = XHS(context, {"allowed_umos": allowed})
        plugins = {"codex_bridge": bridge, "long_term_memory": memory, "reminder": reminder,
                   "travel_assistant": travel, "xhs_search": xhs}
        context.get_all_stars = lambda: [SimpleNamespace(name=name, activated=True, star_cls=instance)
                                       for name, instance in plugins.items()]
        await memory.initialize()
        try:
            handlers = [registered(bridge, "archive_chat_message"), registered(bridge, "route_directly_to_codex"),
                        registered(memory, "explicit_memory_request"), registered(bridge, "capture_reply_recipient"),
                        registered(xhs, "natural_login"), registered(xhs, "natural_search")]
            handlers.sort(key=lambda handler: -handler.extras_configs["priority"])
            assert [handler.handler_name for handler in handlers[:3]] == [
                "archive_chat_message", "route_directly_to_codex", "explicit_memory_request"]
            metadata = {type(instance).__module__: SimpleNamespace(name=name, display_name=name)
                        for name, instance in plugins.items()}
            request.set_extra("activated_handlers", handlers)
            with patch.dict(star_map, metadata), patch.object(bridge, "_submit", new=AsyncMock()) as submitted:
                async for unused in StarRequestSubStage().process(request):
                    pass
                submitted.assert_awaited_once_with(request, request.message_str, "read-only")
            assert request.is_stopped()
            assert bridge._history_store.database.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
            assert memory.db.execute("SELECT count(*) FROM memories").fetchone()[0] == 0
            request.send.assert_not_awaited()
            passed("real registered handler order archives first and stops business handlers before any memory mutation")

            link = "https://www.xiaohongshu.com/discovery/item/synthetic-note?xsec_token=synthetic-access"
            source = event(link)
            source.set_extra("codex_first_submitted", True)
            with patch.object(memory, "add_memory_context", new=AsyncMock()) as memory_hook, patch.object(
                reminder, "provide_schedule_tool", new=AsyncMock()
            ) as reminder_hook, patch.object(xhs, "provide_search_tool", new=AsyncMock()) as xhs_hook, patch.object(
                travel, "provide_tools", new=AsyncMock()
            ) as travel_hook:
                registry = await CapabilityRegistry(context, source, history=bridge).prepare()
            for hook in (memory_hook, reminder_hook, xhs_hook, travel_hook):
                hook.assert_not_awaited()
            names = {schema["function"]["name"] for schema in registry.schemas(codex=True)}
            assert {"manage_long_term_memory", "list_my_long_term_memories", "create_schedule_reminders",
                    "xhs_read_notes", "xhs_search", "travel_search_tickets", "list_current_chat_history"} <= names
            assert not {"xhs_login", "xhs_logout", "xhs_login_status"} & names
            passed("direct-mode catalog exposes scoped tools without running legacy intent or eager-memory hooks")

            raw = {"feed_id": "synthetic-note", "data": {"note": {
                "noteId": "synthetic-note", "title": "Synthetic video", "desc": "正文" * 7000,
                "type": "video", "xsecToken": "synthetic-access", "imageList": [],
                "video": {"media": {"stream": {"new-codec": [{"masterUrl": "https://media.example.com/video.mp4?sign=resource-signature"}]}},
                          "subtitles": {"zh-CN": [{"url": "https://media.example.com/subtitle.vtt"}]}},
                "future_field": {"safe": [1, 2], "authorization": "synthetic-private-header"}},
                "comments": {"list": [{"content": "synthetic public comment"}], "has_more": True}}}
            client = XHSClient(endpoint="http://127.0.0.1:18060", token_file="unused")
            client._request = Mock(side_effect=lambda *args, **kwargs: copy.deepcopy(raw))
            with patch.object(xhs, "_client", return_value=client), patch("xhs_search.main.download_image", new=AsyncMock()) as download:
                result = document(await registry.execute_codex("xhs_read_notes", {"include_images": False}, "raw-first"))
                assert result["ok"]
                note = result["notes"][0]
                assert note["body_truncated"] and len(note["source"]["data"]["note"]["desc"]) == 14000
                assert note["source"]["data"]["note"]["video"] == raw["data"]["note"]["video"]
                assert note["source"]["data"]["note"]["future_field"] == {"safe": [1, 2]}
                assert "synthetic-access" not in json.dumps(result) and "synthetic-private-header" not in json.dumps(result)
                assert "仅读取文案和封面" not in result["notice"]
                assert not registry.codex_deliveries("raw-first")
                download.assert_not_awaited()
                raw["data"]["note"]["future_field"]["safe"] = [3]
                fresh = document(await registry.execute_codex("xhs_read_notes", {"include_images": False}, "raw-second"))
                assert fresh["notes"][0]["source"]["data"]["note"]["future_field"]["safe"] == [3]
            passed("trusted tool returns full source video/subtitles and unknown fields, strips secrets, and does not prefetch or reuse stale content")

            restored = restore_event(snapshot_event(source))
            assert restored.get_extra("codex_first_submitted") is True
            restored_registry = await CapabilityRegistry(context, restored, history=bridge).prepare()
            with patch.object(xhs, "_client", return_value=client):
                recovered = document(await restored_registry.execute_codex("xhs_read_notes", {"include_images": False}, "recovered"))
            assert recovered["ok"] and "source" in recovered["notes"][0]
            passed("durable request restoration preserves direct-mode tool access without carrying a forgeable trust credential")

            normal = event(link)
            ordinary = await CapabilityRegistry(context, normal).prepare()
            with patch.object(xhs, "_call", new=AsyncMock()) as rejected:
                assert document(await ordinary.execute("xhs_read_notes", {"include_images": False}, "ordinary"))["code"] == "notes_intent_required"
                for arguments in ({"include_source": True}, {"_codex_call": True}, {"sender": "other"}):
                    assert (await registry.execute_codex("xhs_read_notes", arguments, "forged-" + next(iter(arguments))))["code"] == "invalid_arguments"
                foreign = event(link, group="foreign")
                unauthorized = await CapabilityRegistry(context, foreign).prepare()
                assert not (await unauthorized.execute_codex("xhs_read_notes", {}, "foreign"))["ok"]
            rejected.assert_not_awaited()
            passed("ordinary calls, forged fields and other conversations cannot acquire trusted source access")

            real = event("接着办")
            async def rpc(payload):
                assert payload["op"] == "submit"
                assert "[Codex工具执行策略]" in payload["prompt"] and "原请求接收时间：" in payload["prompt"]
                assert payload["mode"] == "read-only"
                return {"ok": True, "job": {"id": payload["job_id"], "state": "queued", "mode": "read-only",
                        "created_at": datetime.now(timezone.utc).isoformat()}}
            bridge._rpc = AsyncMock(side_effect=rpc)
            await bridge.route_directly_to_codex(real)
            bridge._rpc.assert_awaited_once()
            assert real.is_stopped() and real.get_extra("codex_first_submitted")
            assert any("xhs_read_notes" == tool["name"] for tool in bridge._rpc.call_args.args[0]["capability_tools"])
            passed("real submission keeps original task identity, current date, complete tool catalog and read-only workspace mode")

            for text in ("/xhs login", "/codex status", "不要用 Codex，直接回答"):
                skipped = event(text)
                with patch.object(bridge, "_submit", new=AsyncMock()) as submitted:
                    await bridge.route_directly_to_codex(skipped)
                    submitted.assert_not_awaited()
                    assert not skipped.is_stopped()
            context.send_message.assert_not_awaited()
            passed("control commands and explicit Codex opt-out retain their existing route; no actual QQ transport is invoked")
        finally:
            await bridge.terminate()
            await memory.terminate()
            await travel.terminate()
        report = {"ok": True, "real_framework": True, "provider_network": False, "qq_messages_sent": 0,
                  "production_mutations": 0, "checks": CHECKS, "count": len(CHECKS)}
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

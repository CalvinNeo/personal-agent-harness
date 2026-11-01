import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ.get("MEMORY_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "plugins")))
from codex_bridge.main import Main as Bridge
from codex_bridge.core import owner_key
from codex_bridge.memory import with_memory_context
from intent_router.main import Main as Router
from long_term_memory.main import Main as Memory


def event(text, *, private=False, sender="synthetic-owner", reply=None):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message = [Plain(text)] + ([reply] if reply else [])
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = "" if private else "synthetic-group"
    message.self_id = "synthetic-bot"
    message.message_id = "synthetic-" + str(id(message))
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), sender if private else message.group_id)
    source.is_at_or_wake_command = True
    source.send = AsyncMock()
    return source


def timeout(source):
    source.set_extra("intent_router_timeout", {"event": id(source), "owner": owner_key(source.unified_msg_origin, source.get_sender_id()),
                                              "text": hashlib.sha256(source.message_str.encode()).hexdigest()})


async def main():
    report = {"ok": False, "real_framework": True, "provider_network": False, "qq_messages_sent": 0,
              "real_runner_jobs_created": 0, "production_memory_writes": 0, "checks": []}
    destination = Path(os.environ["MEMORY_REPORT"])
    source = event("公司在合成园区甲楼。家在合成社区乙区。以上要加入长期记忆。", private=True)
    allowed = [source.unified_msg_origin, event("").unified_msg_origin]
    provider = SimpleNamespace(text_chat=AsyncMock())
    context = SimpleNamespace(get_all_stars=Mock(return_value=[]), get_provider_by_id=Mock(return_value=provider), send_message=AsyncMock())
    with tempfile.TemporaryDirectory(prefix="sqlite-memory-", dir=destination.parent) as directory:
        database = Path(directory) / "memory.sqlite3"
        memory = Memory(context, {"allowed_umos": allowed, "allowed_senders": ["synthetic-owner"], "memory_file": str(database)})
        bridge = Bridge(context, {"allowed_umos": allowed, "allowed_senders": ["synthetic-owner"]})
        bridge._rpc = AsyncMock(side_effect=AssertionError("memory must not call runner"))
        router = Router(context, {"mode": "light", "allowed_umos": allowed, "timeout_seconds": 2})
        context.get_all_stars.return_value = [SimpleNamespace(name="long_term_memory", activated=True, star_cls=memory)]
        await memory.initialize()
        try:
            await memory.explicit_memory_request(source)
            await router.classify_group_message(source)
            timeout(source)
            await bridge.natural_status(source)
            request = ProviderRequest(prompt=source.message_str)
            await bridge.provide_delegation_tool(source, request)
            assert source.is_stopped() and source.get_extra("long_term_memory_tool_done")
            assert request.func_tool is None
            provider.text_chat.assert_not_awaited()
            bridge._rpc.assert_not_awaited()
            rows = memory._rows(memory._owner(source))
            assert len(rows) == 1 and "合成园区甲楼" in rows[0][1] and "以上要加入" not in rows[0][1]
            assert database.read_bytes().startswith(b"SQLite format 3")
            source.send.assert_awaited_once()
            report["checks"].append("private suffix directive commits SQLite before planner and cannot enter timeout or semantic Codex route")

            await memory.explicit_memory_request(source)
            source.send.assert_awaited_once()
            await memory.terminate()
            await memory.initialize()
            assert memory._rows(memory._owner(source)) == rows
            assert database.stat().st_mode & 0o777 == 0o600
            assert not list(Path(directory).rglob("*.md"))
            report["checks"].append("restart retains database row, duplicate handler does not send again, and no Markdown memory file exists")

            for question in ("你记得我住在哪里、办公地点在哪里吗？", "明天从家出发去单位，帮我规划路线"):
                followup = event(question, private=True)
                request = ProviderRequest(prompt=question)
                await memory.add_memory_context(followup, request)
                assert rows[0][1] in request.system_prompt
                assert request.func_tool is None
                assert rows[0][1] in with_memory_context(context, followup, question, memory.logger)
                followup.send.assert_not_awaited()
            assert memory._rows(memory._owner(source)) == rows
            bridge._rpc.assert_not_awaited()
            report["checks"].append("location aliases reach ordinary chat and Codex after restart without writes, model calls or QQ sends")

            copied = event("办公地点", private=True, sender="synthetic-other")
            copied.set_extra("long_term_memory_request", source.get_extra("long_term_memory_request"))
            await memory.explicit_memory_request(copied)
            assert not copied.is_stopped() and not copied.send.called
            assert memory.memory_context_for_task(copied, "办公地点") == ""
            assert memory.memory_context_for_task(event("办公地点"), "办公地点") == ""
            report["checks"].append("private and group scopes remain separate; other sender and copied event cannot read or write the memory")

            group = event("记住合成偏好：回答简洁")
            await memory.explicit_memory_request(group)
            assert group.is_stopped() and len(memory._rows(memory._owner(group))) == 1
            report["checks"].append("original group-memory path still commits independently")

            report_source = event("将该报告单记入长期记忆", private=True)
            await memory.explicit_memory_request(report_source)
            assert report_source.get_extra("long_term_memory_request") and not report_source.is_stopped()
            await router.classify_group_message(report_source)
            timeout(report_source)
            await bridge.natural_status(report_source)
            request = ProviderRequest(prompt=report_source.message_str)
            await memory.add_memory_context(report_source, request)
            await bridge.provide_delegation_tool(report_source, request)
            assert request.func_tool is None and "SQLite" in request.system_prompt and "请其附上报告" in request.system_prompt
            assert len(memory._rows(memory._owner(source))) == 1
            report["checks"].append("report request reserves local route before planning and missing attachment cannot invent a saved record")

            direct = event("合成偏好：喝温水。以上要加入长期记忆", private=True,
                           reply=Reply(id="synthetic-reply", message_str="Codex 任务 #12345678abcdef01"))
            timeout(direct)
            await bridge.natural_status(direct)
            await memory.explicit_memory_request(direct)
            assert direct.is_stopped() and len(memory._rows(memory._owner(source))) == 2
            bridge._rpc.assert_not_awaited()
            report["checks"].append("trusted memory plugin recognizes request even before hook reservation and old Codex reply cannot steal it")

            for text in ("不要记住合成偏好", "例如合成偏好。以上要加入长期记忆", "合成偏好。以上要加入长期记忆吗？"):
                denied = event(text, private=True)
                await memory.explicit_memory_request(denied)
                assert not denied.is_stopped() and not denied.get_extra("long_term_memory_request")
            assert len(memory._rows(memory._owner(source))) == 2
            report["checks"].append("negation, examples and questions remain non-authorizing")

            handlers = [handler for handler in star_handlers_registry if handler.handler_name == "explicit_memory_request"]
            assert len(handlers) == 1 and handlers[0].extras_configs["priority"] == 130
            filters = getattr(handlers[0], "event_filters", [])
            types = [item for item in filters if "EventMessageType" in type(item).__name__]
            assert types and all(item.filter(current, {}) for item in types for current in (source, group))
            assert memory.db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            report["checks"].append("real AstrBot event filter registers both group and private above planner/Codex priority")
            report.update({"ok": True, "count": len(report["checks"]), "storage": "sqlite", "markdown_files_created": 0})
        finally:
            await memory.terminate()
            await router.terminate()
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())

"""Actual AstrBot/QQ encoder, with transport mocked and synthetic identities."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import At, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.platform.sources.qqofficial import qqofficial_platform_adapter as adapter
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ["QQ_FIX_PLUGIN_ROOT"])
from codex_bridge.main import Main
from codex_bridge.semantic import semantic_eligible


def event(text, *, targets=(), sender="synthetic-owner", group="synthetic-group", reply=None):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message = [At(qq="synthetic-bot"), Plain(text), *[At(qq=value) for value in targets]]
    if reply:
        message.message.append(reply)
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id, message.self_id, message.message_id = group, "synthetic-bot", "synthetic-message"
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock(return_value=True)
    return source


async def main():
    checks = []
    with tempfile.TemporaryDirectory(dir=os.environ["QQ_FIX_STATE_ROOT"]) as directory:
        origin = event("").unified_msg_origin
        context = SimpleNamespace(send_message=AsyncMock(), get_all_stars=Mock(return_value=[]))
        bridge = Main(context, {"allowed_umos": [origin]})
        bridge.routes_file = Path(directory) / "jobs.json"
        bridge._rpc, bridge._submit = AsyncMock(), AsyncMock()
        source = event("at 我老婆，告诉她胖了", reply=Reply(id="synthetic-reply", message_str="【Codex 任务 #12345678abcdef01】"))
        source.set_extra("intent_router_timeout", {
            "event": id(source), "owner": hashlib.sha256((origin + "\0synthetic-owner").encode()).hexdigest(),
            "text": hashlib.sha256(source.message_str.encode()).hexdigest(),
        })
        await bridge.natural_status(source)
        await bridge.natural_status(source)
        source.send.assert_awaited_once()
        text = source.send.call_args.args[0].chain[0].text
        assert source.is_stopped() and "QQ 原生 @" in text and "无法直接发送" not in text
        bridge._rpc.assert_not_awaited()
        bridge._submit.assert_not_awaited()
        assert not semantic_eligible(source.message_str)
        checks.append("exact incident clarifies recipient before timeout and quoted Codex continuation")
        for phrase in ("at 我老婆，告诉她准备好了", "告诉她准备好了"):
            source = event(phrase, targets=("synthetic-member",))
            await bridge.mention_current_sender(source)
            chain = source.send.call_args.args[0]
            assert chain.use_markdown_ and chain.chain[0].qq == "synthetic-member"
            encoded = await QQOfficialMessageEvent._parse_to_qqofficial(chain)
            assert encoded[0] == '<qqbot-at-user id="synthetic-member" />准备好了'
        checks.append("native At target and body encode with the installed QQ adapter")
        raw = adapter.PatchedGroupMessage(None, "synthetic-event", {
            "id": "synthetic-native-message",
            "content": '<qqbot-at-user id="synthetic-bot"/> <qqbot-at-user id="synthetic-member"/> 告诉她准备好了',
            "timestamp": "2026-11-03T11:00:00+08:00", "group_openid": "synthetic-group",
            "author": {"member_openid": "synthetic-owner", "username": "synthetic-user"},
            "mentions": [{"id": "synthetic-bot", "username": "bot", "is_you": True},
                         {"id": "synthetic-member", "username": "member", "is_you": False}],
            "attachments": [],
        })
        incoming = await adapter.QQOfficialPlatformAdapter._parse_from_qqofficial(raw, MessageType.GROUP_MESSAGE)
        source = AstrMessageEvent(incoming.message_str, incoming, PlatformMetadata("qq_official", "test", "test"), "synthetic-group")
        source.is_at_or_wake_command, source.send = True, AsyncMock(return_value=True)
        await bridge.mention_current_sender(source)
        chain = source.send.call_args.args[0]
        assert chain.chain[0].qq == "synthetic-member"
        assert (await QQOfficialMessageEvent._parse_to_qqofficial(chain))[0] == '<qqbot-at-user id="synthetic-member" />准备好了'
        checks.append("real incoming SDK member metadata survives the QQ adapter and selects the correct target")
        source = event("测试一下 at 我")
        await bridge.mention_current_sender(source)
        assert source.send.call_args.args[0].chain[0].qq == "synthetic-owner"
        checks.append("existing current-sender mention remains compatible")
        source = event("at 我老婆，告诉她准备好了", reply=Reply(id="quoted-member", chain=[At(qq="synthetic-member")]))
        await bridge.mention_current_sender(source)
        assert "仅凭称呼" in source.send.call_args.args[0].chain[0].text
        source = event("at 我", group="foreign-group")
        await bridge.mention_current_sender(source)
        source.send.assert_not_awaited()
        context.send_message.assert_not_awaited()
        checks.append("quoted targets and foreign groups cannot authorize sending")
        source = event("")
        source.set_extra("voice_input_transcribed", True)
        source.set_extra("voice_input_transcript", "at 我")
        await bridge.natural_status(source)
        assert source.send.call_args.args[0].chain[0].qq == "synthetic-owner"
        checks.append("voice transcription remains on the same current-group path")
        handlers = [handler for handler in star_handlers_registry if handler.handler_module_path == "codex_bridge.main" and handler.handler_name == "mention_current_sender"]
        assert len(handlers) == 1 and handlers[0].extras_configs["priority"] > 110
        checks.append("installed framework registers local mention before the light planner")
        report = {"ok": True, "checks": checks, "qq_messages_sent": 0, "real_codex_jobs_created": 0}
        Path(os.environ["QQ_FIX_REPORT"]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)


asyncio.run(main())

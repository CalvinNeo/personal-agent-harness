"""Actual AstrBot/QQ encoder, with transport mocked and synthetic identities."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botpy.api import BotAPI
from botpy.errors import ServerError

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.event import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent, MessageSesion
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.platform.sources.qqofficial import qqofficial_platform_adapter as adapter
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ["QQ_FIX_PLUGIN_ROOT"])
from codex_bridge.main import Main
from codex_bridge.semantic import semantic_eligible
from xianyu_watch.main import Main as Watch


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


def synthetic_baseline(scope):
    ids = [str(700000000000000000 + index) for index in range(1, 41)]
    return {
        "id": "12345678abcdef01", "umo": scope, "sender": "synthetic-creator",
        "owner": hashlib.sha256((scope + "\0synthetic-creator").encode()).hexdigest(),
        "keywords": ["合成商品"], "interval_minutes": 30, "max_results": 20,
        "max_price": 240, "state": "active", "created_at": 2_000_000_000,
        "next_run_at": 2_000_001_800, "baseline_ready": True, "baseline_ids": ids,
        "baseline_legacy": True, "baseline_at": None, "seen_ids": {item: 2_000_000_000 for item in ids},
        "pending_alerts": [], "generation": 1,
        "baseline_snapshots": {item: {"item_id": item, "title": f"合成商品 {index:02d} [小号]",
            "price": f"{100 + index}.00", "url": "https://h5.m.goofish.com/item?id=" + item,
            "observed_at": 2_000_000_000, "recovered_at": 2_000_000_000, "source": "recovered_current_query"}
            for index, item in enumerate(ids, 1)},
    }


class InterceptedSDKHTTP:
    """The real BotAPI builds JSON; only its final HTTP transport is replaced."""
    def __init__(self, *, reject_markdown=False):
        self.payloads = []
        self.reject_markdown = reject_markdown

    async def request(self, route, *, json=None, **unused):
        assert route.method == "POST" and route.url.endswith("/v2/groups/synthetic-group/messages")
        self.payloads.append(dict(json))
        if self.reject_markdown and json.get("markdown"):
            raise ServerError("不允许发送原生 markdown")
        return {"id": "synthetic-sdk-receipt-" + str(len(self.payloads))}


def sdk_event(http):
    raw = adapter.PatchedGroupMessage(None, "synthetic-event", {
        "id": "synthetic-passive-source", "content": "展示完整合成商品基线",
        "timestamp": "2030-01-01T00:00:00+08:00", "group_openid": "synthetic-group",
        "author": {"member_openid": "synthetic-reader", "username": "synthetic-user"},
        "mentions": [], "attachments": [],
    })
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message = [Plain("展示完整合成商品基线")]
    message.message_str = "展示完整合成商品基线"
    message.sender = MessageMember("synthetic-reader")
    message.group_id, message.self_id, message.message_id = "synthetic-group", "synthetic-bot", "synthetic-passive-source"
    message.raw_message = raw
    source = QQOfficialMessageEvent(message.message_str, message, PlatformMetadata("qq_official", "test", "test"),
                                    "synthetic-group", SimpleNamespace(api=BotAPI(http)))
    source.is_at_or_wake_command = True
    return source


async def verify_complete_markdown_payloads(directory, checks):
    expected_urls = {"https://h5.m.goofish.com/item?id=" + str(700000000000000000 + index) for index in range(1, 41)}
    summaries = []
    # Suppress framework metrics/base hooks as well as the SDK HTTP boundary;
    # no real network, QQ message or runtime state is touched by this probe.
    with patch.object(AstrMessageEvent, "send", new=AsyncMock()), \
         patch.object(adapter.Platform, "send_by_session", new=AsyncMock()):
        for path in ("event", "proactive"):
            http = InterceptedSDKHTTP()
            source = sdk_event(http)
            store = Watch(SimpleNamespace(send_message=AsyncMock()), {
                "allowed_umos": [source.unified_msg_origin], "state_file": str(Path(directory) / (path + "-watches.json"))})
            record = synthetic_baseline(source.unified_msg_origin)
            store.records[record["id"]] = record
            original_send = source.send
            chains = []
            if path == "proactive":
                platform = object.__new__(adapter.QQOfficialPlatformAdapter)
                platform.client = SimpleNamespace(api=BotAPI(http))
                platform.use_markdown_default = False
                platform._session_last_message_id = {"synthetic-group": "synthetic-cached-message"}
                platform._session_scene = {"synthetic-group": "group"}
                platform._allow_group_proactive_send = True
                session = MessageSesion("test", MessageType.GROUP_MESSAGE, "synthetic-group")
            async def send(chain):
                assert chain.use_markdown_ is True and chain.use_t2i_ is False
                assert all(isinstance(part, Plain) for part in chain.chain)
                chains.append(chain)
                if path == "event":
                    return await original_send(chain)
                return await platform._send_by_session_common(session, chain)
            source.send = send
            result = await store._show_baseline(source, record["id"])
            assert result["ok"] and result["delivery_plan"]["total_items"] == 40
            assert len(chains) == len(http.payloads) == result["delivery_plan"]["chunk_count"] == 4
            actual_urls, numbers = [], []
            for chain, payload in zip(chains, http.payloads):
                assert payload["msg_type"] == 2 and payload.get("markdown") and not payload.get("content")
                text = payload["markdown"]["content"]
                assert text == "".join(part.text for part in chain.chain)
                assert len(text.encode("utf-8")) <= 3500
                assert r"\[小号\]" in text and "补查" in text
                actual_urls.extend(re.findall(r"\]\((https://h5\.m\.goofish\.com/item\?id=[0-9]+)\)", text))
                numbers.extend(int(value) for value in re.findall(r"(?m)^([0-9]+)\. \[", text))
                assert chain.qqofficial_sent_message_ids, "SDK receipt was not bound back to its original page"
                assert bool(payload.get("msg_id")) is (path == "event")
            assert len(actual_urls) == len(set(actual_urls)) == 40 and set(actual_urls) == expected_urls
            assert numbers == list(range(1, 41))
            assert not store.file.exists(), "show tool mutated monitor storage"
            summaries.append({"path": path, "pages": len(chains), "items": len(numbers), "links": len(actual_urls),
                              "explicit_markdown": True, "sdk_json_intercepted": True})
        # Demonstrate what the adapter does on a *synthetic* server rejection.
        # This is not evidence that production QQ rejected Markdown.
        denied_http = InterceptedSDKHTTP(reject_markdown=True)
        denied_event = sdk_event(denied_http)
        chain = MessageChain([Plain("[合成商品](https://h5.m.goofish.com/item?id=700001)")], use_markdown_=True)
        await denied_event.send(chain)
        assert len(denied_http.payloads) == 3
        assert denied_http.payloads[0]["msg_id"] and not denied_http.payloads[1].get("msg_id")
        fallback = denied_http.payloads[-1]
        assert fallback["msg_type"] == 0 and not fallback.get("markdown")
        assert fallback["content"] == chain.chain[0].text and "](https://" in fallback["content"]
    checks.append("real show tool emits all 40 synthetic rows as four explicit-Markdown pages through the event adapter and real BotAPI JSON boundary")
    checks.append("proactive adapter preserves all 40 short-label hyperlink targets even when its default Markdown setting is false")
    checks.append("synthetic Markdown rejection retries without msg_id then falls back to unchanged plain markup; this does not verify client rendering")
    return {"sdk_payloads_verified": summaries, "synthetic_plain_fallback_verified": True,
            "real_qq_api_acceptance_verified": False, "qq_client_render_verified": False}


async def main():
    checks = []
    with tempfile.TemporaryDirectory(dir=os.environ["QQ_FIX_STATE_ROOT"]) as directory:
        payload_report = await verify_complete_markdown_payloads(directory, checks)
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
        report = {"ok": True, "checks": checks, "qq_messages_sent": 0, "real_codex_jobs_created": 0,
                  "network_calls": 0, **payload_report}
        Path(os.environ["QQ_FIX_REPORT"]).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)


asyncio.run(main())

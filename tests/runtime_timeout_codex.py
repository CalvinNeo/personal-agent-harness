import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from astrbot.api.message_components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest

sys.path.insert(0, os.environ.get("ROUTER_PLUGIN_ROOT", str(Path(__file__).resolve().parents[1] / "plugins")))
from codex_bridge.main import Main as Bridge
from intent_router.main import Main as Router


def event(text, *, sender="synthetic-owner", group="synthetic-group"):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message = [Plain(text)]
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = group
    message.self_id = "synthetic-bot"
    message.message_id = "synthetic-" + str(time.monotonic_ns())
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", "test"), group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock(return_value=True)
    return source


def response(route="chat", confidence=0.99):
    return SimpleNamespace(completion_text=json.dumps({"route": route, "confidence": confidence, "task": "合成任务",
                                                       "mode": "read-only", "missing_context": []}, ensure_ascii=False))


async def main():
    report = {"ok": False, "real_framework": True, "provider_network": False, "runner_rpc": "mocked",
              "qq_messages_sent": 0, "checks": []}
    source = event("请解释 Python 闭包并给个简单例子")
    provider = SimpleNamespace(text_chat=AsyncMock(return_value=response()))
    context = SimpleNamespace(get_provider_by_id=Mock(return_value=provider), send_message=AsyncMock(), get_all_stars=Mock(return_value=[]))
    config = {"mode": "light", "allowed_umos": [source.unified_msg_origin], "allowed_senders": ["synthetic-owner"], "timeout_seconds": 2.0}
    router = Router(context, config)
    bridge = Bridge(context, config)
    released = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(**arguments):
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            await released.wait()
        return response("chat")

    async def runner(payload):
        assert payload["op"] == "submit" and payload["mode"] == "read-only"
        return {"ok": True, "job": {"id": payload["job_id"], "state": "queued", "phase": "queued", "mode": "read-only"}}

    bridge._rpc = AsyncMock(side_effect=runner)
    try:
        with tempfile.TemporaryDirectory(prefix="timeout-codex-", dir=Path(os.environ["ROUTER_REPORT"]).parent) as directory:
            bridge.routes_file = Path(directory) / "jobs.json"
            provider.text_chat.side_effect = slow
            started = time.monotonic()
            await router.classify_group_message(source)
            elapsed = time.monotonic() - started
            assert 1.9 <= elapsed < 2.4
            assert source.get_extra("intent_router_plan") is None and source.get_extra("intent_router_timeout")
            await cancelled.wait()
            await bridge.natural_status(source)
            bridge._rpc.assert_awaited_once()
            payload = bridge._rpc.call_args.args[0]
            assert payload["prompt"] == source.message_str and payload["mode"] == "read-only"
            assert source.is_stopped() and source.get_extra("codex_timeout_submitted")
            source.send.assert_awaited_once()
            chain = source.send.call_args.args[0]
            assert "已受理 Codex 只读任务" in "".join(part.text for part in chain.chain if isinstance(part, Plain))
            report["timeout_to_codex_ms"] = round(elapsed * 1000)
            report["checks"].append("real 2-second deadline automatically enters real read-only submission path and returns mocked acceptance board")

            await asyncio.gather(bridge.natural_status(source), bridge.natural_status(source))
            request = ProviderRequest(prompt=source.message_str)
            await bridge.provide_delegation_tool(source, request)
            assert bridge._rpc.await_count == 1 and not request.func_tool
            released.set()
            await asyncio.gather(*router._pending)
            await asyncio.sleep(0)
            assert source.get_extra("intent_router_plan") is None and bridge._rpc.await_count == 1
            report["checks"].append("duplicate handler and model-tool paths cannot resubmit; late planner output is discarded")

            provider.text_chat.side_effect = None
            for reply in (response(), response("codex", confidence=0.55), SimpleNamespace(completion_text="invalid")):
                provider.text_chat.return_value = reply
                current = event("请解释 Python 闭包并给个简单例子")
                await router.classify_group_message(current)
                await bridge.natural_status(current)
                assert not current.get_extra("intent_router_timeout") and bridge._rpc.await_count == 1
            report["checks"].append("on-time chat, low confidence and invalid JSON are not mislabeled as timeout")

            binding = source.get_extra("intent_router_timeout")
            current = event(source.message_str)
            current.set_extra("intent_router_timeout", binding)
            await bridge.natural_status(current)
            denied = event("不用 Codex，请解释 Python 闭包并给个简单例子")
            local = Router(context, {**config, "timeout_seconds": 0.05})
            provider.text_chat.side_effect = AsyncMock(side_effect=TimeoutError())
            await local.classify_group_message(denied)
            await bridge.natural_status(denied)
            assert bridge._rpc.await_count == 1
            await local.terminate()
            report["checks"].append("event-bound timeout cannot be copied and explicit Codex opt-out remains effective")

            for extra in ("travel_local_request", "xhs_notes_request"):
                current = event("请总结已经读取的资料并保留真实图片")
                await router.classify_group_message(current)
                current.set_extra(extra, True)
                await bridge.natural_status(current)
                assert bridge._rpc.await_count == 1
            for text in ("十分钟后提醒我喝水", "检查小红书的登录态", "例如帮我研究机器人框架"):
                current = event(text)
                await router.classify_group_message(current)
                await bridge.natural_status(current)
                assert bridge._rpc.await_count == 1
            report["checks"].append("local image-summary, travel, login, reminder and quoted/example instruction protections remain in force")

            unauthorized = event("请解释 Python 闭包并给个简单例子", sender="synthetic-other")
            unauthorized.set_extra("intent_router_timeout", binding)
            await bridge.natural_status(unauthorized)
            assert bridge._rpc.await_count == 1
            context.send_message.assert_not_awaited()
            report.update({"ok": True, "count": len(report["checks"]), "mocked_runner_submissions": bridge._rpc.await_count,
                           "mode": payload["mode"], "late_result_rejected": True, "real_runner_jobs_created": 0})
    finally:
        released.set()
        await router.terminate()
        target = Path(os.environ["ROUTER_REPORT"])
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        target.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

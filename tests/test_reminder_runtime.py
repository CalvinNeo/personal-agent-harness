"""Run against the installed AstrBot venv, with only fake outbound transport."""
import asyncio
import json
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from unittest.mock import AsyncMock, patch

from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star_handler import star_handlers_registry
from astrbot.api.message_components import Plain, At, Reply
from reminder.main import Main, AddressedGroup


def event(text, sender="alice", group="test-group", addressed=True, parts=None):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.message = parts if parts is not None else [Plain(text)]
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
        state = Path(folder) / "state.json"
        cfg = {"allowed_umos": [event("").unified_msg_origin], "reminder_file": str(state)}
        context = type("FakeContext", (), {"send_message": AsyncMock(return_value=True)})()
        plugin = Main(context, cfg)
        schema = json.loads((Path(__file__).resolve().parents[1] / "plugins/reminder/_conf_schema.json").read_text())
        default = AstrBotConfig(str(Path(folder) / "config.json"), schema=schema)
        assert default["allowed_umos"] == []
        assert not Main(context, default)._allowed(event(""))
        assert AddressedGroup().filter(event(""), {})
        assert not AddressedGroup().filter(event("", addressed=False), {})
        for text, kwargs in [
            ("两分钟后提醒我喝水", {"addressed": False}),
            ("两分钟后提醒我喝水", {"group": "unapproved"}),
            ("今天天气如何", {}),
            ("不要两分钟后提醒我喝水", {}),
            ('“两分钟后提醒我喝水”', {}),
            ("/codex 两分钟后提醒我喝水", {}),
        ]:
            e = event(text, **kwargs)
            await plugin.natural_reminder(e)
            assert not e.is_stopped() and not e.send.called
            assert not plugin._reminders
        # Text appended from a reply/STT must not execute as a direct request.
        e = event("两分钟后提醒我喝水", parts=[Plain("看看这个")])
        await plugin.natural_reminder(e)
        assert not e.is_stopped() and not plugin._reminders
        e = event("过一会提醒我喝水")
        await plugin.natural_reminder(e)
        assert e.is_stopped() and e.send.called and not plugin._reminders
        e = event("两分钟后提醒我喝水")
        await plugin.natural_reminder(e)
        assert e.is_stopped() and e.send.call_count == 1
        record = json.loads(state.read_text())[0]
        assert record["sender"] == "alice" and record["text"] == "喝水"
        assert 115 <= record["due_at"] - time.time() <= 120
        assert state.stat().st_mode & 0o777 == 0o600
        restored = Main(context, cfg)
        restored._load()
        assert restored._reminders == plugin._reminders
        other = event("", sender="bob")
        await plugin._cancel(other, "取消 " + record["id"])
        assert len(plugin._reminders) == 1
        await plugin.cancel_reminder(event(""), record["id"])
        assert not plugin._reminders and not plugin._live_events
        await plugin.remind(event(""), "半分钟后提醒我喝水")
        assert len(plugin._reminders) == 1
        # Real scheduler, fake transport: Markdown At must survive delivery.
        plugin._reminders[0]["due_at"] = time.time() - 1
        async def fake_sleep(seconds):
            if seconds == 1:
                plugin._closed = True
        with patch("reminder.main.asyncio.sleep", fake_sleep):
            await plugin._scheduler()
        assert not plugin._reminders and context.send_message.call_count == 1
        sent_chain = context.send_message.call_args.args[1]
        assert sent_chain.use_markdown_ and isinstance(sent_chain.chain[0], At)
        # Missing adapter must retain restored reminders and retry.
        plugin._closed = False
        plugin._reminders = [{**record, "due_at": time.time() - 1}]
        plugin._live_events = {}
        context.send_message.return_value = False
        with patch("reminder.main.asyncio.sleep", fake_sleep):
            await plugin._scheduler()
        assert len(plugin._reminders) == 1 and plugin._reminders[0]["attempts"] == 1
        assert plugin._reminders[0]["due_at"] > time.time()
        metadata = [h for h in star_handlers_registry if h.handler_module_path == "reminder.main"]
        assert len(metadata) == 4, [h.handler_name for h in metadata]
        natural = next(h for h in metadata if h.handler_name == "natural_reminder")
        assert natural.extras_configs["priority"] == 90
        assert all(f.filter(event(""), {}) for f in natural.event_filters)
        assert not all(f.filter(event("", addressed=False), {}) for f in natural.event_filters)
        print("PASS reminder runtime: real imports/schema/registration, authorization, routing, persistence, ownership, Markdown delivery, retry")


asyncio.run(main())

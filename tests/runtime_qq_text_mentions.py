import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import At, Reply
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star_handler import star_handlers_registry
from botpy.connection import ConnectionState


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("MENTION_PLUGIN_ROOT", str(ROOT / "plugins")))
from intent_router.main import Main as Router
from reminder.main import Main as Reminder, _request_text


def load_adapter():
    candidate = os.environ.get("MENTION_ADAPTER_ROOT")
    if candidate:
        directory = Path(candidate)
        package = "astrbot.core.platform.sources.qqofficial"
        for name, filename in (
            (package + ".text_mentions", "text_mentions.py"),
            (package + ".qqofficial_platform_adapter", "qqofficial_platform_adapter.py"),
        ):
            spec = importlib.util.spec_from_file_location(name, directory / filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return module
    from astrbot.core.platform.sources.qqofficial import qqofficial_platform_adapter
    return qqofficial_platform_adapter


def payload(text, *, group="synthetic-group", mentions=None, **extra):
    return {
        "id": "synthetic-message", "content": text,
        "timestamp": "2026-11-01T00:00:00+08:00", "group_openid": group,
        "author": {"member_openid": "synthetic-owner", "username": "synthetic-user"},
        "mentions": mentions or [], "attachments": [], **extra,
    }


def make_event(message):
    source = AstrMessageEvent(
        message.message_str, message,
        PlatformMetadata("qq_official", "test", "test"), message.session_id,
    )
    source.send = AsyncMock()
    return source


async def main():
    report = {
        "ok": False, "real_framework": True, "qq_messages_sent": 0,
        "provider_requests": 0, "real_runner_jobs_created": 0,
        "production_data_writes": 0, "checks": [],
    }
    adapter = load_adapter()
    configuration = {
        "id": "test", "type": "qq_official", "enable": False,
        "appid": "synthetic-app", "secret": "",
        "enable_group_c2c": True, "enable_guild_direct_message": False,
        "use_markdown": False, "text_mention_aliases": ["示例智能体"],
    }
    settings = {
        "ignore_bot_self_message": True, "ignore_at_all": True,
        "friend_message_needs_wake_prefix": False, "no_permission_reply": False,
        "enable_id_white_list": True, "id_whitelist": ["synthetic-group"],
        "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False,
        "id_whitelist_log": False,
    }
    platform = adapter.QQOfficialPlatformAdapter(configuration, settings, asyncio.Queue())
    platform.client._commit = Mock()
    context = SimpleNamespace(
        astrbot_config={
            "wake_prefix": ["/"], "admins_id": [], "disable_builtin_commands": True,
            "plugin_set": ["*"], "platform_settings": settings,
        },
        db_helper=None, astrbot_config_id="synthetic-config",
    )
    waking = WakingCheckStage()
    whitelist = WhitelistCheckStage()
    await waking.initialize(context)
    await whitelist.initialize(context)
    reminder_handlers = [
        handler for handler in star_handlers_registry
        if handler.handler_module_path == "reminder.main" and handler.handler_name == "natural_reminder"
    ]
    assert len(reminder_handlers) == 1

    async def receive(text, *, native=False, **extra):
        message = adapter.PatchedGroupMessage(None, "synthetic-event", payload(text, **extra))
        platform.client._commit.reset_mock()
        callback = platform.client.on_group_at_message_create if native else platform.client.on_group_message_create
        await callback(message)
        platform.client._commit.assert_called_once()
        return make_event(platform.client._commit.call_args.args[0])

    async def enter_pipeline(source):
        with patch.object(star_handlers_registry, "get_handlers_by_event_type", return_value=reminder_handlers), patch(
            "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
            new=AsyncMock(side_effect=lambda event, handlers: handlers),
        ):
            await waking.process(source)
        if not source.is_stopped():
            await whitelist.process(source)
        return source

    try:
        assert platform.text_mention_aliases == ("示例智能体",)
        assert callable(ConnectionState.parse_group_message_create)
        assert callable(ConnectionState.parse_group_at_message_create)
        assert platform.intents.public_messages and not platform.intents.guild_messages
        report["checks"].append("explicit per-platform aliases preserve existing QQ intent permissions")

        for prefix in ("@示例智能体 ", "＠示例智能体：", "@示例智能体\u2005"):
            source = await enter_pipeline(await receive(prefix + "你好"))
            assert source.is_at_or_wake_command and not source.is_stopped()
            assert source.message_str == _request_text(source) == "你好"
            assert len([part for part in source.get_messages() if isinstance(part, At)]) == 1
            assert next(part for part in source.get_messages() if isinstance(part, At)).qq == source.get_self_id()
        report["checks"].append("delivered ordinary group text produces a real At and matching cleaned Plain text before wake checks")

        source = await enter_pipeline(await receive("@示例智能体"))
        assert source.is_at_or_wake_command and not source.is_stopped()
        assert source.message_str == _request_text(source) == ""
        source = await enter_pipeline(await receive("@示例智能体 /help"))
        assert source.is_at_or_wake_command and not source.is_stopped()
        assert source.message_str == "help" and _request_text(source) == "/help"
        report["checks"].append("empty mentions and slash commands retain framework handling")

        for text in ("你好", "@其他人 你好", "@示例智能体二号 你好", "提到 @示例智能体 你好", "> @示例智能体 你好", "`@示例智能体 你好`"):
            source = await enter_pipeline(await receive(text))
            assert not source.is_at_or_wake_command and source.is_stopped()
            assert not any(isinstance(part, At) for part in source.get_messages())
        source = await enter_pipeline(await receive("看看引用", message_type=103, msg_elements=[{"id": "synthetic-quoted", "content": "@示例智能体 十分钟后提醒我喝水"}]))
        assert source.is_stopped() and not source.is_at_or_wake_command
        assert any(isinstance(part, Reply) for part in source.get_messages())
        report["checks"].append("other names, similar names, examples, middle mentions and quoted content never become a text-alias wake")

        source = await enter_pipeline(await receive("@示例智能体 十分钟后提醒我喝水", group="synthetic-denied-group"))
        assert source.is_at_or_wake_command and source.is_stopped()
        report["checks"].append("the existing group whitelist still rejects an addressed unauthorized group")

        platform.text_mention_aliases = ()
        source = await enter_pipeline(await receive("@示例智能体 你好"))
        assert not source.is_at_or_wake_command and source.is_stopped()
        platform.text_mention_aliases = ("示例智能体",)
        report["checks"].append("missing alias configuration leaves the previous behavior unchanged")

        for text in ("你好", "@示例智能体 你好"):
            source = await enter_pipeline(await receive(text, native=True))
            assert source.is_at_or_wake_command and not source.is_stopped()
            assert source.message_str == _request_text(source) == "你好"
        source = await enter_pipeline(await receive("@其他人 你好", native=True))
        assert source.is_at_or_wake_command and not source.is_stopped()
        assert source.message_str == _request_text(source) == "@其他人 你好"
        native_mentions = [{"id": "synthetic-bot", "username": "native-bot", "is_you": True}]
        source = await enter_pipeline(await receive('<qqbot-at-user id="synthetic-bot"/> 你好', mentions=native_mentions))
        assert source.is_at_or_wake_command and not source.is_stopped()
        assert source.message_str == _request_text(source) == "你好"
        report["checks"].append("native QQ @ callbacks and bot mention metadata remain compatible")

        private_payload = payload("@示例智能体 你好")
        private_payload["author"] = {"user_openid": "synthetic-owner", "username": "synthetic-user"}
        private_message = adapter.PatchedC2CMessage(None, "synthetic-event", private_payload)
        platform.client._commit.reset_mock()
        await platform.client.on_c2c_message_create(private_message)
        source = make_event(platform.client._commit.call_args.args[0])
        assert source.message_str == _request_text(source) == "@示例智能体 你好"
        with patch.object(star_handlers_registry, "get_handlers_by_event_type", return_value=[]), patch(
            "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
            new=AsyncMock(return_value=[]),
        ):
            await waking.process(source)
        assert source.is_at_or_wake_command and not source.is_stopped()
        report["checks"].append("private chat still wakes normally and its text is not rewritten by group aliases")

        with tempfile.TemporaryDirectory(prefix="qq-text-mention-", dir=os.environ["MENTION_STATE_ROOT"]) as directory:
            provider = SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(
                completion_text=json.dumps({"route": "chat", "confidence": 0.99, "task": "你好", "mode": "read-only", "missing_context": []})
            )))
            plugin_context = SimpleNamespace(
                send_message=AsyncMock(), get_all_stars=Mock(return_value=[]),
                get_provider_by_id=Mock(return_value=provider),
            )
            source = await enter_pipeline(await receive("@示例智能体 十分钟后提醒我喝水"))
            allowed = [source.unified_msg_origin]
            reminder = Reminder(plugin_context, {
                "allowed_umos": allowed, "allowed_senders": ["synthetic-owner"],
                "reminder_file": str(Path(directory) / "reminders.json"),
            })
            assert any(handler.handler_name == "natural_reminder" for handler in source.get_extra("activated_handlers"))
            await reminder.natural_reminder(source)
            assert source.is_stopped() and len(reminder._reminders) == 1
            assert reminder._reminders[0]["text"] == "喝水"
            source.send.assert_awaited_once()
            assert (Path(directory) / "reminders.json").is_file()
            report["checks"].append("real reminder filter and parser consume cleaned top-level text and persist only a temporary reminder")

            source = await enter_pipeline(await receive("@示例智能体 十分钟后提醒我喝水"))
            source.message_obj.sender.user_id = "synthetic-denied-sender"
            before = copy.deepcopy(reminder._reminders)
            await reminder.natural_reminder(source)
            assert reminder._reminders == before and not source.is_stopped()
            source.send.assert_not_awaited()
            report["checks"].append("plugin sender restrictions remain enforced after text-alias wake")

            source = await enter_pipeline(await receive("@示例智能体 你好"))
            router = Router(plugin_context, {"mode": "light", "allowed_umos": allowed})
            try:
                await router.classify_group_message(source)
                assert source.get_extra("intent_router_attempted")
                assert source.get_extra("intent_router_route") == "chat"
                provider.text_chat.assert_awaited_once()
                assert provider.text_chat.call_args.kwargs["prompt"].find("@示例智能体") == -1
            finally:
                await router.terminate()
            plugin_context.send_message.assert_not_awaited()
            report["checks"].append("ordinary chat reaches the real intent router using cleaned text and a mocked provider")
        report["ok"] = True
    finally:
        report["count"] = len(report["checks"])
        destination = Path(os.environ["MENTION_REPORT"])
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

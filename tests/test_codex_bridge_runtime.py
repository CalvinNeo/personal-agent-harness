"""Installed AstrBot integration with fake socket transport and QQ sends."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))

from astrbot.api.message_components import Plain
from astrbot.core.config import AstrBotConfig
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from codex_bridge.main import Main


def event(sender="alice"):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE
    msg.message = [Plain("/codex example task")]
    msg.message_str = "/codex example task"
    msg.sender = MessageMember(sender)
    msg.group_id = "synthetic-group"
    msg.self_id = "bot"
    msg.message_id = "synthetic-id"
    value = AstrMessageEvent(msg.message_str, msg, PlatformMetadata("qq_official", "test", "test"), "synthetic-group")
    value.is_at_or_wake_command = True
    value.send = AsyncMock()
    return value


async def main():
    with tempfile.TemporaryDirectory() as folder:
        schema = json.loads((Path(__file__).resolve().parents[1] / "plugins/codex_bridge/_conf_schema.json").read_text())
        cfg = AstrBotConfig(str(Path(folder) / "config.json"), schema=schema)
        plugin = Main(None, cfg)
        denied = event()
        await plugin.codex(denied, "test")
        assert denied.is_stopped() and not denied.send.called and plugin._task is None
        cfg["allowed_umos"] = [event().unified_msg_origin]
        cfg["socket_path"] = "/tmp/synthetic-bridge.sock"
        refused = event()
        await plugin.codex(refused, "write test")
        assert refused.send.call_count == 1 and plugin._task is None
        reader = AsyncMock()
        reader.readline.return_value = b'{"ok": true, "result": "Synthetic result"}\n'
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        with patch("codex_bridge.main.asyncio.open_unix_connection", AsyncMock(return_value=(reader, writer))) as connect:
            accepted = event()
            await plugin.codex(accepted, "test task")
            await plugin._task
            assert accepted.send.call_count == 2  # Accepted + result, both original QQ event.
            connect.assert_awaited_once_with("/tmp/synthetic-bridge.sock")
            payload = json.loads(writer.write.call_args.args[0])
            assert payload["mode"] == "read-only" and len(payload["workspace"]) == 32
            assert payload["prompt"] == "test task"
            writer.close.assert_called_once()
        await plugin.terminate()
        print("PASS Codex bridge runtime: imports/schema, default deny, write gate, configured socket, original-event async reply")


asyncio.run(main())

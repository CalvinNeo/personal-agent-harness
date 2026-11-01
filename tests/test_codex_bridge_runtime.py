"""Installed AstrBot integration with fake socket transport and QQ sends."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
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
        context = SimpleNamespace(get_all_stars=lambda: [], send_message=AsyncMock())
        plugin = Main(context, cfg)
        plugin.routes_file = Path(folder) / "jobs.json"
        denied = event()
        await plugin.codex(denied, "test")
        assert denied.is_stopped() and not denied.send.called and not plugin.routes
        cfg["allowed_umos"] = [event().unified_msg_origin]
        cfg["socket_path"] = "/tmp/synthetic-bridge.sock"
        refused = event()
        await plugin.codex(refused, "write test")
        assert refused.send.call_count == 1 and not plugin.routes
        reader = AsyncMock()
        writer = MagicMock()
        writer.drain = AsyncMock()
        writer.wait_closed = AsyncMock()
        def response():
            payload = json.loads(writer.write.call_args.args[0])
            if payload["op"] == "delivery":
                assert payload["delivery"] == "delivered"
                return b'{"ok": true}\n'
            state = "queued" if payload["op"] == "submit" else "completed"
            job = {"id": payload["job_id"], "state": state, "phase": state, "mode": "read-only",
                   "result": "Synthetic result" if state == "completed" else None,
                   "artifacts": [], "delivery": "pending"}
            return (json.dumps({"ok": True, "job": job}) + "\n").encode()
        reader.readline.side_effect = response
        with patch("codex_bridge.main.asyncio.open_unix_connection", AsyncMock(return_value=(reader, writer))) as connect:
            accepted = event()
            await plugin.codex(accepted, "test task")
            assert accepted.send.call_count == 1 and len(plugin.routes) == 1
            connect.assert_awaited_once_with("/tmp/synthetic-bridge.sock", limit=4 * 1024 * 1024)
            payload = json.loads(writer.write.call_args.args[0])
            assert payload["mode"] == "read-only" and len(payload["workspace"]) == 32
            assert payload["prompt"] == "test task"
            assert "test task" not in plugin.routes_file.read_text()
            await plugin._poll_once()
            assert accepted.send.call_count == 2 and connect.await_count == 3
            assert next(iter(plugin.routes.values()))["acked"]
            assert "Synthetic result" in accepted.send.call_args.args[0].chain[0].text
            assert writer.close.call_count == 3
        context.send_message.assert_not_awaited()
        await plugin.terminate()
        print("PASS Codex bridge runtime: imports/schema, default deny, write gate, configured socket, original-event async reply")


asyncio.run(main())

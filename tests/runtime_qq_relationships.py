"""Read production relationship memory; all QQ sends and runner calls are mocked."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
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

sys.path.insert(0, os.environ["QQ_RELATION_PLUGIN_ROOT"])
from codex_bridge.main import Main as Bridge
from long_term_memory.main import Main as Memory


def source(text, route, *, sender=None, origin=None, quoted=None):
    umo = origin or route["umo"]
    platform_id, _, group = umo.split(":", 2)
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message = [At(qq="synthetic-bot"), Plain(text)]
    if quoted:
        message.message.append(Reply(id="synthetic-reply", message_str="【Codex 任务 #" + quoted + "】"))
    message.message_str, message.message_id = text, "synthetic-regression"
    message.sender = MessageMember(sender or route["sender"])
    message.self_id, message.group_id = "synthetic-bot", group
    event = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "test", platform_id), group)
    event.is_at_or_wake_command = True
    event.send = AsyncMock(return_value=True)
    return event


async def main():
    state = Path(os.environ["QQ_RELATION_STATE"])
    recovered = json.loads((state / "recovered-routes.json").read_text())["jobs"]
    owned = [route for route in recovered if route.get("local_mention", {}).get("target_label") == "我老婆"]
    assert owned, "fingerprint-verified incident routes were not recovered"
    route = next(route for route in owned if route.get("parent_job_id"))
    config = json.loads(Path("/var/lib/astrbot/data/config/long_term_memory_config.json").read_text())
    context = SimpleNamespace(send_message=AsyncMock(side_effect=AssertionError("no real transport")))
    memory = Memory(context, config)
    memory.db = sqlite3.connect("file:" + str(memory.db_path) + "?mode=ro", uri=True)
    memory.db.execute("PRAGMA query_only=ON")
    context.get_all_stars = Mock(return_value=[SimpleNamespace(name="long_term_memory", activated=True, star_cls=memory)])
    def snapshot():
        return hashlib.sha256(json.dumps(memory.db.execute("SELECT * FROM memories ORDER BY id").fetchall(), ensure_ascii=False).encode()).hexdigest()
    before = snapshot()
    checks = []
    try:
        event = source("at 我老婆，告诉她胖了", route)
        facts = memory.saved_relationship_facts(event)
        match = re.search(r"<@!?" + re.escape(route["sender"]) + r">\s*是\s*<@!?([A-Za-z0-9_-]+)>\s*的\s*(?:老公|丈夫)", "\n".join(facts))
        assert match, "actual explicitly saved spouse statement was not found"
        expected_member = match[1]
        with tempfile.TemporaryDirectory(dir=state) as temporary:
            bridge = Bridge(context, {"allowed_umos": [route["umo"]]})
            bridge.routes_file = Path(temporary) / "jobs.json"
            bridge.routes = {item["id"]: item for item in recovered}
            bridge._rpc, bridge._submit = AsyncMock(), AsyncMock()
            await bridge.natural_status(event)
            chain = event.send.call_args.args[0]
            assert chain.chain[0].qq == expected_member and chain.chain[1].text == "胖了"
            assert chain.use_markdown_ and event.is_stopped()
            assert (await QQOfficialMessageEvent._parse_to_qqofficial(chain))[0] == '<qqbot-at-user id="' + expected_member + '" />胖了'
            checks.append("original request resolves the exact account from the existing production spouse fact")
            for item in owned:
                event = source("再试试呢", item, quoted=item["id"])
                await bridge.mention_current_sender(event)
                await bridge.natural_status(event)
                event.send.assert_awaited_once()
                chain = event.send.call_args.args[0]
                assert chain.chain[0].qq == expected_member and chain.chain[1].text == "胖了"
            checks.append("all recovered incident replies retry locally with the original body")
            bridge._rpc.assert_not_awaited()
            bridge._submit.assert_not_awaited()
            checks.append("direct request and quoted retry never call the Codex runner")
            for other in (source("at 我老婆", route, sender="synthetic-other-owner"),
                          source("at 我老婆", route, origin="private_qq_bot:GroupMessage:synthetic-foreign-group")):
                assert memory.saved_relationship_facts(other) == []
            checks.append("other senders and groups cannot use this relationship memory")
            denied = source("再试试呢", route, sender="synthetic-other-owner", quoted=route["id"])
            await bridge.mention_current_sender(denied)
            denied.send.assert_not_awaited()
            checks.append("quoting another owner's incident cannot trigger the local message")
            assert snapshot() == before and memory.db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            context.send_message.assert_not_awaited()
            checks.append("production SQLite is query-only and unchanged; actual QQ transport is disabled")
        report = {"ok": True, "checks": checks, "recovered_routes": len(owned), "qq_messages_sent": 0,
                  "real_codex_jobs_created": 0, "private_content_printed": False}
        (state / (os.environ.get("QQ_RELATION_MODE", "candidate") + "-runtime.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        memory.db.close()
        memory.db = None


asyncio.run(main())

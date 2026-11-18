"""Real plugin and broker checks with synthetic data and disabled QQ/network."""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from PIL import Image as PillowImage
from astrbot.api.message_components import Image, Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata

sys.path.insert(0, os.environ["TRUST_PLUGIN_ROOT"])
from codex_bridge.capabilities import CapabilityRegistry
from codex_bridge.main import Main as Bridge
from codex_bridge.recipients import ReplyRecipientContext
from long_term_memory.main import Main as Memory
from reminder.main import Main as Reminder
from travel_assistant.main import Main as Travel
from xhs_search.main import Main as XHS
from xhs_search.notes import validate_image


CHECKS = []
SERIAL = 0
TZ = ZoneInfo("Asia/Shanghai")


def event(text="接着办", *, sender="synthetic-owner", group="synthetic-group", private=False):
    global SERIAL
    SERIAL += 1
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message, message.message_str = [Plain(text)], text
    message.sender = MessageMember(sender)
    message.group_id = "" if private else group
    message.self_id, message.message_id = "synthetic-bot", "synthetic-trust-" + str(SERIAL)
    source = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "trust", "trust"), sender if private else group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock(return_value=True)
    return source


def document(result):
    if isinstance(result, str):
        return json.loads(result)
    if "_mcp_result" in result:
        return json.loads(next(part["text"] for part in result["_mcp_result"]["content"] if part["type"] == "text"))
    return result


def passed(message):
    CHECKS.append(message)
    print("PASS " + message, flush=True)


async def main():
    destination = Path(os.environ["TRUST_REPORT"])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codex-tool-trust-", dir=destination.parent) as directory:
        root = Path(directory)
        source, private = event(), event(private=True)
        allowed = [source.unified_msg_origin, private.unified_msg_origin]
        context = SimpleNamespace(send_message=AsyncMock(side_effect=AssertionError("QQ transport disabled")), get_all_stars=lambda: [])
        memory = Memory(context, {"allowed_umos": allowed, "memory_file": str(root / "memory.sqlite3")})
        reminder = Reminder(context, {"allowed_umos": allowed, "reminder_file": str(root / "reminders.json")})
        travel = Travel(context, {"allowed_umos": allowed, "data_dir": str(root / "travel")})
        xhs = XHS(context, {"allowed_umos": allowed, "login_umos": [private.unified_msg_origin], "login_senders": [private.get_sender_id()]})
        metadata = [SimpleNamespace(name=name, activated=True, star_cls=plugin) for name, plugin in (
            ("long_term_memory", memory), ("reminder", reminder), ("travel_assistant", travel), ("xhs_search", xhs))]
        context.get_all_stars = lambda: metadata
        await memory.initialize()
        try:
            travel.http.get = AsyncMock(side_effect=AssertionError("provider network disabled"))
            travel.rail.search = AsyncMock(return_value=([], {"provider": "synthetic-rail", "observed_at": "synthetic", "source": "synthetic"}))
            travel.flights.search = AsyncMock(side_effect=AssertionError("provider network disabled"))
            note = {"note_id": "synthetic-note", "title": "合成原帖", "body": "真实工具返回的合成正文", "kind": "normal",
                    "url": "https://www.xiaohongshu.com/explore/synthetic-note", "image_urls": ["https://sns-webpic-qc.xhscdn.com/synthetic"]}
            stream = io.BytesIO()
            PillowImage.new("RGB", (24, 24), "red").save(stream, format="PNG")
            picture = validate_image(stream.getvalue())
            operations = []

            async def upstream(method, *arguments, **keywords):
                operations.append(method)
                if method == "search_bundle":
                    return {"results": [{"title": note["title"], "url": note["url"], "snippet": "作者"}],
                            "references": {"synthetic-note": {"feed_id": "synthetic-note", "xsec_token": "synthetic-access"}}}
                if method == "note_detail":
                    return copy.deepcopy(note)
                if method == "login_details":
                    return {"is_logged_in": True, "login_pending": False}
                if method == "login_qr":
                    return stream.getvalue()
                if method == "logout":
                    return None
                raise AssertionError("unexpected upstream operation")

            xhs._call = AsyncMock(side_effect=upstream)
            registry = await CapabilityRegistry(context, source).prepare()
            names = {schema["function"]["name"] for schema in registry.schemas(codex=True)}
            assert {"xhs_search", "xhs_read_notes", "manage_long_term_memory", "create_schedule_reminders",
                    "cancel_current_reminder", "travel_search_tickets", "travel_filter_tickets", "travel_map", "travel_capabilities"} <= names
            assert not {"xhs_login", "xhs_logout", "xhs_login_status"} & names
            assert "_codex_call" not in json.dumps(registry.schemas(codex=True))
            normal = await registry.execute("xhs_read_notes", {"query": "合成主题"}, "normal-xhs")
            assert normal["code"] == "notes_intent_required"
            prepared = document(await registry.execute_codex("xhs_read_notes", {"query": "合成主题"}, "codex-search"))
            assert prepared["status"] == "references_ready"
            with patch("xhs_search.main.download_image", new=AsyncMock(return_value=picture)):
                result = await registry.execute_codex("xhs_read_notes", prepared["next_arguments"], "codex-note")
            assert document(result)["notes"][0]["body"] == note["body"]
            assert "synthetic-access" not in json.dumps(result)
            chains = registry.codex_deliveries("codex-note")
            assert len(chains) == 1 and any(isinstance(part, Image) for part in chains[0].chain)
            assert not registry.codex_deliveries("codex-note")
            passed("keywordless Codex search/read executes and preserves native images; ordinary calls retain intent guard")

            denied_after = await registry.execute("xhs_read_notes", {}, "normal-after")
            assert denied_after["code"] == "notes_intent_required"
            source.set_extra("codex", True)
            source.set_extra("_codex_call", True)
            assert document(await xhs.semantic_read_notes(source))["code"] == "notes_intent_required"
            for field in ("_codex_call", "codex", "owner", "sender", "umo"):
                forged = await registry.execute_codex("xhs_read_notes", {field: True}, "forged-" + field)
                assert forged["code"] == "invalid_arguments"
            passed("completed Codex calls and forged event/JSON flags cannot grant trust to ordinary invocations")

            memory_arguments = {"action": "add", "content": "合成偏好：回答简洁"}
            ordinary = await registry.execute("manage_long_term_memory", memory_arguments, "normal-memory")
            assert ordinary["code"] == "intent_required"
            saved = await registry.execute_codex("manage_long_term_memory", memory_arguments, "codex-memory")
            assert saved["ok"]
            assert (await registry.execute_codex("manage_long_term_memory", memory_arguments, "codex-memory-retry"))["ok"]
            assert memory._count_for_owner(memory._owner(source)) == 1
            updated = await registry.execute_codex("manage_long_term_memory", {
                "action": "update", "target": "合成偏好", "content": "合成偏好：用简体中文"}, "codex-update")
            assert updated["ok"]
            secret = await registry.execute_codex("manage_long_term_memory", {"action": "add", "content": "密码：synthetic-secret"}, "codex-sensitive")
            assert not secret["ok"]
            foreign = event(sender="synthetic-other")
            foreign_registry = await CapabilityRegistry(context, foreign).prepare()
            foreign_delete = await foreign_registry.execute_codex("manage_long_term_memory", {"action": "delete", "target": "合成偏好"}, "foreign-delete")
            assert not foreign_delete["ok"] and memory._count_for_owner(memory._owner(source)) == 1
            assert (await registry.execute_codex("manage_long_term_memory", {"action": "delete", "target": "合成偏好"}, "codex-delete"))["ok"]
            assert (await registry.execute_codex("manage_long_term_memory", {"action": "add", "content": "另一条合成记忆"}, "codex-add-again"))["ok"]
            assert (await registry.execute_codex("manage_long_term_memory", {"action": "clear"}, "codex-clear"))["ok"]
            assert memory._count_for_owner(memory._owner(source)) == 0
            passed("Codex memory mutations bypass wording gates, remain idempotent, reject secrets and preserve sender ownership")

            planned = (datetime.now(TZ) + timedelta(hours=2)).isoformat(timespec="seconds")
            schedule = {"items": [{"text": "合成喝水提醒", "event_at": planned}], "advance_minutes": 10}
            ordinary_schedule = await registry.execute("create_schedule_reminders", schedule, "normal-schedule")
            assert ordinary_schedule["code"] == "intent_required"
            created = await registry.execute_codex("create_schedule_reminders", schedule, "codex-schedule")
            assert created["ok"] and len(reminder._reminders) == 1
            assert (await registry.execute_codex("create_schedule_reminders", schedule, "codex-schedule-repeat"))["ok"]
            second = {"items": [{"text": "合成休息提醒", "event_at": planned}], "advance_minutes": 0}
            created_second = await registry.execute_codex("create_schedule_reminders", second, "codex-schedule-second")
            assert created_second["ok"] and len(reminder._reminders) == 2
            assert created_second["created"][0]["id"] != created["created"][0]["id"]
            denied_cancel = await foreign_registry.execute_codex("cancel_current_reminder", {"reminder_id": created["created"][0]["id"]}, "foreign-cancel")
            assert not denied_cancel["ok"] and len(reminder._reminders) == 2
            cancelled = await registry.execute_codex("cancel_current_reminder", {"reminder_id": created["created"][0]["id"]}, "codex-cancel")
            assert cancelled["ok"] and len(reminder._reminders) == 1
            for position, invalid in enumerate((
                {"items": [{"text": "过去的提醒", "event_at": "2020-01-01T09:00:00+08:00"}], "advance_minutes": 0},
                {"items": [{"text": "@everyone", "event_at": planned}], "advance_minutes": 0},
                {"items": [{"text": "合成", "event_at": "明天上午"}], "advance_minutes": 0},
                {"items": schedule["items"], "advance_minutes": -1},
            )):
                assert not (await registry.execute_codex("create_schedule_reminders", invalid, "invalid-schedule-" + str(position)))["ok"]
            assert len(reminder._reminders) == 1
            passed("Codex schedule/cancel uses supplied arguments without phrase gates; times, mentions, scopes and duplicate receipts remain guarded")

            scheduled_event = event("请根据日程设置提醒，提前10分钟")
            ordinary_created = document(await reminder.create_schedule_reminders(scheduled_event, **schedule))
            assert ordinary_created["ok"]
            handed_off = await CapabilityRegistry(context, scheduled_event).prepare()
            before_handoff = len(reminder._reminders)
            recovered = await handed_off.execute_codex("create_schedule_reminders", schedule, "existing-schedule")
            assert recovered["ok"] and len(reminder._reminders) == before_handoff
            assert recovered["created"][0]["id"] == ordinary_created["created"][0]["id"]
            far_future = (datetime.now(TZ) + timedelta(days=30)).isoformat(timespec="seconds")
            far_schedule = {"items": [{"text": "合成远期提醒", "event_at": far_future}], "advance_minutes": 0}
            assert reminder.max_delay is None
            assert (await registry.execute_codex("create_schedule_reminders", far_schedule, "far-schedule"))["ok"]
            reminder.max_delay = 3600
            limited = await registry.execute_codex("create_schedule_reminders", {
                "items": [{"text": "配置范围限制", "event_at": far_future}], "advance_minutes": 0}, "limited-schedule")
            assert not limited["ok"]
            reminder.max_delay = None
            passed("handoff reuses already saved schedules; existing unlimited horizon and explicit administrator time limits survive")

            short = event("好")
            short_registry = await CapabilityRegistry(context, short).prepare()
            assert "travel_capabilities" not in {schema["function"]["name"] for schema in short_registry.schemas()}
            assert (await short_registry.execute_codex("travel_capabilities", {}, "capabilities"))["ok"]
            ticket_arguments = {"kind": "train", "origin": "上海虹桥", "destination": "北京南",
                                "departure_date": (datetime.now(TZ) + timedelta(days=1)).date().isoformat()}
            tickets = await short_registry.execute_codex("travel_search_tickets", ticket_arguments, "tickets")
            assert tickets["ok"]
            assert (await short_registry.execute_codex("travel_filter_tickets", {"criteria": {"max_price": 500},
                    "result_set_id": tickets["result_set_id"]}, "filter"))["ok"]
            foreign_filter = await foreign_registry.execute_codex("travel_filter_tickets", {"criteria": {},
                    "result_set_id": tickets["result_set_id"]}, "foreign-filter")
            assert not foreign_filter["ok"]
            assert (await short_registry.execute_codex("travel_map", {"action": "search", "query": "上海虹桥站"}, "map"))["ok"]
            passed("one-character Codex followup exposes all read-only travel tools while foreign result sets remain inaccessible")

            account_registry = await CapabilityRegistry(context, private).prepare()
            assert (await account_registry.execute_codex("xhs_login_status", {}, "status"))["state"] == "logged_in"
            assert (await account_registry.execute("xhs_login", {}, "normal-login"))["code"] == "intent_required"
            assert (await account_registry.execute_codex("xhs_login", {}, "codex-login"))["ok"]
            assert any(isinstance(part, Image) for chain in account_registry.codex_deliveries("codex-login") for part in chain.chain)
            assert (await account_registry.execute_codex("xhs_logout", {}, "codex-logout"))["ok"]
            assert (await registry.execute_codex("xhs_logout", {}, "group-logout"))["code"] == "tool_unavailable"
            other_private = await CapabilityRegistry(context, event(private=True, sender="synthetic-other")).prepare()
            assert not other_private.schemas(codex=True)
            foreign_note = document(await registry.execute_codex("xhs_read_notes", {"note_ids": ["synthetic-foreign-note"]}, "foreign-note"))
            assert not foreign_note["ok"] and foreign_note["failures"][0]["code"] == "note_reference_unavailable"
            passed("Codex account tools bypass phrase gates only in the existing authorized private account; no actual login or logout")

            facts = ["<@synthetic-owner> 是 <@synthetic-wife> 的丈夫"]
            recipient_event = event("按前面的安排继续")
            recipient = ReplyRecipientContext(recipient_event, recipient_event.message_str, facts)
            recipient_registry = await CapabilityRegistry(context, recipient_event, recipient=recipient).prepare()
            ordinary_recipient = await recipient_registry.execute("set_current_reply_recipient", {"target_label": "我老婆"}, "normal-recipient")
            assert not ordinary_recipient["ok"]
            selected = await recipient_registry.execute_codex("set_current_reply_recipient", {"target_label": "我老婆"}, "codex-recipient")
            assert selected["ok"] and recipient.metadata()["target_id"] == "synthetic-wife"
            assert not (await recipient_registry.execute_codex("set_current_reply_recipient", {"target_label": "陌生用户"}, "unknown-recipient"))["ok"]
            assert not (await recipient_registry.execute_codex("set_current_reply_recipient", {"target_label": "全体成员"}, "all-recipient"))["ok"]
            passed("Codex chooses a scoped saved relationship without addressing regex; unknown recipients and all-member mentions remain denied")

            shared = event("https://www.xiaohongshu.com/discovery/item/synthetic-note?xsec_token=synthetic-access")
            bridge = Bridge(context, {"allowed_umos": allowed})
            bridge.routes_file = root / "bridge.json"
            submitted = []

            async def submit(payload):
                assert payload["op"] == "submit"
                submitted.append(payload)
                return {"ok": True, "job": {"id": payload["job_id"], "state": "queued", "conversation_id": payload["job_id"]}}

            bridge._rpc = AsyncMock(side_effect=submit)
            accepted = await bridge._submit(shared, shared.message_str, notify=False)
            assert accepted["ok"] and len(submitted) == 1
            route = bridge.routes[submitted[0]["job_id"]]
            restarted = Bridge(context, bridge.config)
            restarted.routes_file = bridge.routes_file
            restarted._load()
            results = []
            calls = [{"call_id": "a" * 32, "name": "xhs_read_notes", "arguments": {"note_ids": ["synthetic-note"]}}]

            async def broker(payload):
                if payload["op"] == "tool_pending":
                    return {"ok": True, "calls": calls}
                if payload["op"] == "tool_result":
                    results.append(payload["result"])
                    return {"ok": True}
                raise AssertionError("unexpected broker request")

            restarted._rpc = AsyncMock(side_effect=broker)
            no_image_note = {**note, "image_urls": []}
            with patch.object(xhs, "_call", new=AsyncMock(return_value=no_image_note)) as reader:
                await restarted._service_tool_calls(restarted.routes[route["id"]])
                await restarted._service_tool_calls(restarted.routes[route["id"]])
                reader.assert_awaited_once()
            assert len(results) == 2 and all(document(result)["ok"] for result in results)
            assert restarted.routes[route["id"]]["tool_receipts"]["a" * 32]["state"] == "complete"
            context.send_message.assert_not_awaited()
            shared.send.assert_not_awaited()
            await restarted.terminate()
            await bridge.terminate()
            passed("real submitted job after broker restart reads bare share link through execute_codex and replays its durable receipt")

            for change in ("plugin", "sender", "text", "group"):
                request = event()
                protected_registry = await CapabilityRegistry(context, request).prepare()
                if change == "plugin":
                    metadata[-1].activated = False
                elif change == "sender":
                    request.message_obj.sender.user_id = "foreign-sender"
                elif change == "text":
                    request.message_obj.message[0].text = "changed text"
                else:
                    request.unified_msg_origin = "trust:GroupMessage:foreign-group"
                denied = await protected_registry.execute_codex("xhs_search", {"query": "合成"}, "changed-" + change)
                assert not denied["ok"]
                metadata[-1].activated = True
            source.send.assert_not_awaited()
            private.send.assert_not_awaited()
            passed("deactivated plugins and altered sender, group or original text revoke Codex tool authority")
        finally:
            await memory.terminate()
            await travel.terminate()
        report = {"ok": True, "real_framework": True, "provider_network": False, "qq_messages_sent": 0,
                  "production_mutations": 0, "checks": CHECKS, "count": len(CHECKS)}
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        destination.chmod(0o600)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

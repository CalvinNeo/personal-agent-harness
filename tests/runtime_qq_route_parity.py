"""Real AstrBot route parity; production memory is query-only, transports mocked.

QQ_PARITY_PLUGIN_ROOT points at candidate or installed plugin packages.
QQ_PARITY_STATE is a private scratch/report directory, never production state.
No provider network, real runner task, QQ send, or production mutation occurs.
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
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
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.provider.entities import ProviderRequest

sys.path.insert(0, os.environ["QQ_PARITY_PLUGIN_ROOT"])
from codex_bridge.capabilities import CapabilityRegistry
from codex_bridge.core import owner_key
from codex_bridge.main import Main as Bridge
from codex_bridge.recipients import RECIPIENT_TOOL_NAME, ReplyRecipientContext, validate_recipient
from codex_bridge.relationships import resolve_relationship
from long_term_memory.main import Main as Memory
from reminder.main import Main as Reminder
from travel_assistant.main import Main as Travel
from xhs_search.main import Main as XHS


CONFIG_ROOT = Path("/var/lib/astrbot/data/config")
_serial = 0
SOURCE_MANIFEST = (
    "plugins/codex_bridge/main.py", "plugins/codex_bridge/capabilities.py", "plugins/codex_bridge/request_context.py",
    "plugins/codex_bridge/recipients.py", "plugins/codex_bridge/memory.py", "plugins/codex_bridge/metadata.yaml", "plugins/codex_bridge/README.md",
    "plugins/long_term_memory/main.py", "plugins/long_term_memory/metadata.yaml", "plugins/long_term_memory/README.md",
    "plugins/reminder/main.py", "plugins/reminder/metadata.yaml", "plugins/image_context/main.py", "plugins/image_context/metadata.yaml",
    "services/codex-runner.py", "services/task_capabilities.py", "services/task_capabilities_mcp.py", "services/memory_tools.py", "services/memory_mcp.py",
)


def candidate_manifest_digest():
    root = Path(os.environ.get("QQ_PARITY_SOURCE_ROOT", str(Path(os.environ["QQ_PARITY_PLUGIN_ROOT"]).parent)))
    values = {relative: hashlib.sha256((root / relative).read_bytes()).hexdigest() for relative in SOURCE_MANIFEST}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def config(name):
    path = CONFIG_ROOT / (name + "_config.json")
    assert path.is_file(), "required production plugin config is missing"
    value = json.loads(path.read_text())
    assert isinstance(value, dict), "invalid production plugin config"
    return value


def source(text, scope, *, reply=None, voice=False, sender=None):
    global _serial
    _serial += 1
    platform_id, kind, session = scope["umo"].split(":", 2)
    private = kind == "FriendMessage"
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message = [At(qq="synthetic-bot"), Plain("" if voice else text)]
    if reply is not None:
        message.message.append(reply)
    message.message_str = text
    message.message_id = "synthetic-parity-" + str(_serial)
    message.sender = MessageMember(sender or scope["sender"])
    message.self_id = "synthetic-bot"
    message.group_id = "" if private else session
    event = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "parity", platform_id), session)
    event.is_at_or_wake_command = True
    event.send = AsyncMock(return_value=True)
    if voice:
        event.set_extra("voice_input_transcribed", True)
        event.set_extra("voice_input_transcript", text)
    assert event.unified_msg_origin == scope["umo"], "real framework produced a different scope"
    return event


def job(jid, scope, *, parent=None, result="合成解释正文：这是需要生成的内容。", state="running"):
    now = datetime.now(timezone.utc).isoformat()
    return {"id": jid, "owner": owner_key(scope["umo"], scope["sender"]), "mode": "read-only",
            "state": state, "phase": state, "created_at": now, "started_at": now,
            "last_event_at": now, "heartbeat_at": now, "finished_at": now if state == "completed" else None,
            "resumable": True, "conversation_id": parent or jid, "result": result, "artifacts": [], "delivery": "pending"}


class FakeRunner:
    def __init__(self, scope):
        self.scope, self.jobs, self.submits, self.pending, self.tool_results = scope, {}, [], {}, []
        self.rpc = AsyncMock(side_effect=self.call)

    async def call(self, payload):
        operation = payload["op"]
        if operation == "submit":
            self.submits.append(copy.deepcopy(payload))
            jid = payload["job_id"]
            parent = self.jobs.get(payload.get("parent_job_id"), {}).get("conversation_id")
            self.jobs[jid] = job(jid, self.scope, parent=parent)
            return {"ok": True, "job": copy.deepcopy(self.jobs[jid])}
        if operation == "status":
            return {"ok": payload["job_id"] in self.jobs, "job": copy.deepcopy(self.jobs.get(payload["job_id"]))}
        if operation == "tool_pending":
            return {"ok": True, "calls": copy.deepcopy(self.pending.get(payload["job_id"], []))}
        if operation == "tool_result":
            self.tool_results.append(copy.deepcopy(payload))
            return {"ok": True}
        if operation == "delivery":
            return {"ok": True}
        raise AssertionError("unexpected mock runner operation")


def schema_functions(values):
    return sorted((item.get("function", item) for item in values), key=lambda item: item["name"])


def names(values):
    return {value["name"] for value in schema_functions(values)}


async def assert_native_recipient(chain, expected, *, generated=True):
    assert chain.chain and isinstance(chain.chain[0], At), "final reply lacks its native At component"
    assert str(chain.chain[0].qq) == expected, "final reply selected the wrong scoped member"
    assert chain.use_markdown_ is True, "native At must use QQ Markdown"
    text = "".join(part.text for part in chain.chain if isinstance(part, Plain))
    assert text and (not generated or "合成解释正文" in text), "final reply echoed the topic instead of generated content"
    converted = (await QQOfficialMessageEvent._parse_to_qqofficial(chain))[0]
    assert converted.startswith('<qqbot-at-user id="' + expected + '" />'), "real QQ conversion lost the target"


async def main():
    state = Path(os.environ["QQ_PARITY_STATE"])
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    report = {"ok": False, "real_framework": True, "provider_network": False, "qq_messages_sent": 0,
              "real_runner_jobs_created": 0, "production_memory_writes": 0, "private_content_printed": False,
              "checks": []}
    manifest_before = candidate_manifest_digest()
    context = SimpleNamespace(
        send_message=AsyncMock(side_effect=AssertionError("real QQ transport is disabled")),
        get_all_stars=Mock(return_value=[]),
        get_provider_by_id=Mock(return_value=SimpleNamespace(text_chat=AsyncMock(side_effect=AssertionError("provider network is disabled")))),
    )
    memory = Memory(context, config("long_term_memory"))
    # Opening a query-only connection does not run plugin initialize/migrations.
    memory.db = sqlite3.connect("file:" + str(memory.db_path) + "?mode=ro", uri=True)
    memory.db.execute("PRAGMA query_only=ON")
    rows_before = memory.db.execute("SELECT * FROM memories ORDER BY id").fetchall()
    database_before = hashlib.sha256(memory.db_path.read_bytes()).hexdigest()
    logical_before = hashlib.sha256(json.dumps(rows_before, ensure_ascii=False).encode()).hexdigest()
    travel = None
    try:
        bridge_config = config("codex_bridge")
        group = None
        for row in memory.db.execute("SELECT owner, content FROM memories ORDER BY id"):
            for umo in bridge_config.get("allowed_umos", []):
                if ":GroupMessage:" not in umo or umo not in memory.allowed_umos:
                    continue
                for sender in re.findall(r"<@!?([A-Za-z0-9_-]{1,128})>", row[1]):
                    if owner_key(umo, sender) == row[0]:
                        target = resolve_relationship([row[1]], sender, "我老婆")
                        if target:
                            group = {"umo": umo, "sender": sender, "expected": target}
                            break
                if group:
                    break
            if group:
                break
        assert group is not None, "no authorized exact saved spouse relationship was found"
        seed = source("向我老婆讲解一下汗液疗法", group)
        facts = memory.saved_relationship_facts(seed)
        assert resolve_relationship(facts, group["sender"], "我老婆") == group["expected"], "scoped memory selection changed the relationship"
        report["checks"].append("existing production spouse fact resolves through the actual scoped memory plugin")

        with tempfile.TemporaryDirectory(prefix="qq-route-parity-", dir=state) as temporary:
            scratch = Path(temporary)
            reminder_config = config("reminder")
            reminder_config["reminder_file"] = str(scratch / "reminders.json")
            reminder = Reminder(context, reminder_config)
            xhs = XHS(context, config("xhs_search"))
            xhs._call = AsyncMock(side_effect=AssertionError("XHS provider network is disabled"))
            travel_config = config("travel_assistant")
            travel_config["data_dir"] = str(scratch / "travel")
            travel = Travel(context, travel_config)
            for method in ("fetch", "json", "text", "request"):
                if hasattr(travel.http, method):
                    setattr(travel.http, method, AsyncMock(side_effect=AssertionError("travel provider network is disabled")))
            context.get_all_stars.return_value = [SimpleNamespace(name=name, activated=True, star_cls=instance)
                for name, instance in (("long_term_memory", memory), ("reminder", reminder),
                                      ("xhs_search", xhs), ("travel_assistant", travel))]
            bridge = Bridge(context, bridge_config)
            bridge.routes_file = scratch / "jobs.json"
            runner = FakeRunner(group)
            bridge._rpc = runner.rpc

            # Normal generation is decorated only after real generated content exists.
            normal = source("向我老婆讲解一下汗液疗法", group)
            await bridge.capture_reply_recipient(normal)
            await bridge.mention_current_sender(normal)
            normal.send.assert_not_awaited()
            result = normal.plain_result("合成解释正文：这里是完整介绍，不是原样复述话题。")
            result.set_result_content_type(ResultContentType.LLM_RESULT)
            normal.set_result(result)
            await bridge.decorate_reply_recipient(normal)
            await assert_native_recipient(normal.get_result(), group["expected"])
            normal.set_extra("qqofficial_sent_message_ids", ["synthetic-normal-receipt"])
            await bridge.remember_semantic_reply_receipts(normal)
            assert bridge._recipient_receipts, "normal reply did not persist a recipient receipt"
            report["checks"].append("ordinary generated reply prepends the resolved native At and persists its own reply receipt")

            # Quoting an ordinary generated reply uses the same recipient context.
            normal_retry = source("再详细一点", group, reply=Reply(id="synthetic-normal-receipt", message_str="合成解释正文"))
            await bridge.capture_reply_recipient(normal_retry)
            assert bridge._recipient_context(normal_retry).metadata()["target_id"] == group["expected"], "normal reply continuation lost the recipient"
            report["checks"].append("explicit continuation of an ordinary reply inherits only its owner-verified recipient receipt")

            async def submitted(text, *, mode="explicit", quoted=None, voice=False):
                reply = Reply(id="synthetic-task-reply", message_str="【Codex 任务 #" + quoted + "】") if quoted else None
                event = source(text, group, reply=reply, voice=voice)
                before = len(runner.submits)
                await bridge.capture_reply_recipient(event)
                if mode == "explicit":
                    body = re.sub(r"^/(?:codex|cx)\s+", "", text)
                    await bridge.codex(event, body)
                else:
                    if mode == "timeout":
                        event.set_extra("intent_router_timeout", {"event": id(event),
                            "owner": owner_key(event.unified_msg_origin, event.get_sender_id()),
                            "text": hashlib.sha256(text.encode()).hexdigest()})
                    await bridge.natural_status(event)
                assert len(runner.submits) == before + 1, "route failed to submit exactly one mocked task"
                payload = runner.submits[-1]
                route = bridge.routes[payload["job_id"]]
                recipient = validate_recipient(route.get("recipient"), umo=group["umo"], sender_id=group["sender"], owner=route["owner"])
                assert recipient and recipient["target_id"] == group["expected"], "submitted task did not persist the generated-reply recipient"
                completed = {**runner.jobs[route["id"]], "state": "completed", "phase": "completed", "artifacts": []}
                assert await bridge._deliver_final(route, completed), "mock final result failed delivery"
                await assert_native_recipient(event.send.call_args.args[0], group["expected"])
                return event, route, payload

            explicit, explicit_route, _ = await submitted("/codex 给我老婆介绍下汗液疗法")
            report["checks"].append("explicit /codex submission preserves original recipient metadata through actual final QQ conversion")
            _, timeout_route, _ = await submitted("给我老婆介绍下汗液疗法", mode="timeout")
            report["checks"].append("light-planner timeout submission retains the same recipient and generated content")
            _, continuation_route, _ = await submitted("向我老婆讲解一下汗液疗法", mode="reply", quoted=explicit_route["id"])
            _, retry_route, _ = await submitted("再试试呢", mode="reply", quoted=continuation_route["id"])
            assert retry_route["recipient"]["inherited"] is True, "quoted retry recreated an unbound audience"
            report["checks"].append("quoted task continuation and explicit retry carry the same verified recipient without re-entering literal forwarding")
            await submitted("向我老婆讲解一下汗液疗法", mode="timeout", voice=True)
            report["checks"].append("transcribed voice uses the same timeout catalog and recipient path")

            # Retained images remain authoritative report context after a
            # quote continuation; absence of newly uploaded images is normal.
            runner.jobs[explicit_route["id"]]["context_images"] = 1
            retained_report = source("将刚才的报告记入长期记忆", group,
                reply=Reply(id="synthetic-retained-image", message_str="【Codex 任务 #" + explicit_route["id"] + "】"))
            retained_result = await bridge._submit(retained_report, retained_report.message_str,
                                                   parent_job_id=explicit_route["id"])
            assert retained_result.get("ok"), "retained report continuation did not submit"
            retained_registry = bridge._registries[retained_result["job"]["id"]]
            assert retained_registry.event.get_extra("long_term_memory_context_authorized"), "verified retained report images lost memory authorization"

            # The normal model and /codex worker receive the same source-plugin schemas.
            catalogs = []
            for text in ("介绍一下汗液疗法", "查看我的长期记忆", "搜索小红书南京周末攻略", "查明天上海到南京的高铁", "创建明天上午九点开会的提醒"):
                ordinary = source(text, group)
                wrapped = source("/codex " + text, group)
                left = CapabilityRegistry(context, ordinary, recipient=ReplyRecipientContext(ordinary, text, facts))
                right = CapabilityRegistry(context, wrapped, recipient=ReplyRecipientContext(wrapped, "/codex " + text, facts), task_text=text)
                await left.prepare()
                await right.prepare()
                assert schema_functions(left.schemas()) == schema_functions(right.schemas()), "normal/Codex plugin capability schemas diverge"
                catalogs.append(names(left.schemas()))
            combined = set().union(*catalogs)
            assert {"read_long_term_memory", "manage_long_term_memory", "create_schedule_reminders", "list_current_reminders", "xhs_search", RECIPIENT_TOOL_NAME} <= combined, "a configured source-plugin capability is absent"
            assert any(name.startswith("travel_") or name in {"search_tickets", "map_route", "map_search"} for name in combined), "travel capability is absent"
            report["checks"].append("ordinary and explicit Codex catalogs match for memory, reminders, XHS, travel and recipient tools")

            # Mutating capability parity uses only a fresh scratch reminder file.
            reminder_task = "两分钟后提醒我喝水"
            reminder_event = source("/codex " + reminder_task, group)
            reminder_registry = CapabilityRegistry(context, reminder_event, task_text=reminder_task)
            await reminder_registry.prepare()
            assert "create_current_reminder" in names(reminder_registry.schemas()), "simple reminder adapter is missing"
            first_reminder = await reminder_registry.execute("create_current_reminder", {}, "reminder-first")
            second_reminder = await reminder_registry.execute("create_current_reminder", {}, "reminder-replay")
            assert first_reminder.get("ok") and second_reminder.get("ok"), "deterministic reminder capability failed"
            saved_reminders = json.loads(Path(reminder_config["reminder_file"]).read_text())
            assert len(saved_reminders) == 1, "same source request created duplicate scratch reminders"
            reminder_event.send.assert_not_awaited()
            report["checks"].append("explicit Codex reminder tool uses the same parser and creates exactly one scratch reminder across duplicate calls")

            travel_event = source("查询出行工具有哪些能力", group)
            travel_registry = CapabilityRegistry(context, travel_event)
            await travel_registry.prepare()
            travel_result = await travel_registry.execute("travel_capabilities", {}, "travel-read-only")
            assert travel_result.get("ok") and travel_result.get("purchasing") is False, "actual read-only travel capability failed"
            report["checks"].append("shared travel handler reports actual read-only capabilities without provider network")

            # A configured private session gets parity while group-only At stays absent.
            private_umos = [umo for umo in bridge_config.get("allowed_umos", []) if ":FriendMessage:" in umo]
            assert private_umos, "production bridge has no authorized private session to verify"
            private_scope = {"umo": private_umos[0], "sender": private_umos[0].rsplit(":", 1)[-1]}
            private_text = "查看我的长期记忆并检查小红书登录状态"
            private = source(private_text, private_scope)
            private_wrapped = source("/codex " + private_text, private_scope)
            left = CapabilityRegistry(context, private, recipient=ReplyRecipientContext(private, private_text, ()))
            right = CapabilityRegistry(context, private_wrapped, recipient=ReplyRecipientContext(private_wrapped, "/codex " + private_text, ()), task_text=private_text)
            await left.prepare()
            await right.prepare()
            assert schema_functions(left.schemas()) == schema_functions(right.schemas()), "private normal/Codex schemas diverge"
            assert "read_long_term_memory" in names(left.schemas()), "authorized private memory capability is missing"
            assert RECIPIENT_TOOL_NAME not in names(left.schemas()), "group recipient tool leaked into private chat"
            read = await right.execute("read_long_term_memory", {}, "private-read-only")
            assert read.get("status") == "ok", "private shared memory read failed"
            report["checks"].append("authorized private normal/Codex catalogs match and execute scoped memory reads without exposing group At")

            # Exercise the actual bridge tool broker and its replay receipt, not only a registry call.
            cid = "a" * 32
            pending = {"call_id": cid, "name": "read_long_term_memory", "arguments": {}}
            runner.pending[explicit_route["id"]] = [pending]
            await bridge._service_tool_calls(explicit_route)
            assert runner.tool_results[-1]["result"].get("status") == "ok", "broker did not execute actual query-only memory reader"
            first = copy.deepcopy(runner.tool_results[-1]["result"])
            await bridge._service_tool_calls(explicit_route)
            assert runner.tool_results[-1]["result"] == first, "broker replay changed a completed read receipt"
            assert explicit_route["tool_receipts"][cid]["state"] == "complete", "broker receipt was not persisted"
            runner.pending[explicit_route["id"]] = [{"call_id": "b" * 32, "name": RECIPIENT_TOOL_NAME, "arguments": {}}]
            await bridge._service_tool_calls(explicit_route)
            assert runner.tool_results[-1]["result"].get("recipient_set") is True, "broker recipient tool did not share the original event context"
            assert explicit_route["recipient"]["target_id"] == group["expected"], "broker changed the target account"
            report["checks"].append("actual asynchronous broker executes scoped memory/recipient tools and replays durable receipts without duplicate work")

            # Restart restoration reads the original request snapshot, not a model-selected owner.
            restored = Bridge(context, bridge_config)
            restored.routes_file = scratch / "restored-jobs.json"
            restored.routes = {explicit_route["id"]: copy.deepcopy(explicit_route)}
            restored._rpc = runner.rpc
            runner.pending[explicit_route["id"]] = [{"call_id": "c" * 32, "name": "read_long_term_memory", "arguments": {}}]
            await restored._service_tool_calls(restored.routes[explicit_route["id"]])
            assert runner.tool_results[-1]["result"].get("status") == "ok", "restored broker could not execute the captured owner's tool"
            assert restored._registries, "broker did not reconstruct its request-bound registry"
            report["checks"].append("broker restart restores the validated original event and preserves the same authorized catalog")

            bridge._save()
            loaded = Bridge(context, bridge_config)
            loaded.routes_file = bridge.routes_file
            loaded._load()
            assert loaded._recipient_parts(loaded.routes[explicit_route["id"]])[0].qq == group["expected"], "persisted route reload lost its valid native recipient"
            forged = copy.deepcopy(explicit_route)
            forged["recipient"]["sender"] = "synthetic-other-owner"
            assert loaded._recipient_parts(forged) == [], "delivery accepted a recipient copied from another owner"
            denied = source("向我老婆讲解一下汗液疗法", group, sender="synthetic-other-owner")
            assert memory.saved_relationship_facts(denied) == [], "other sender obtained scoped relationship records"
            assert bridge._recipient_context(denied).metadata() is None, "other sender reused the spouse recipient"
            report["checks"].append("route reload validates recipient ownership; copied metadata and other senders cannot reuse the spouse binding")

            assert memory.db.execute("PRAGMA query_only").fetchone() == (1,), "production memory connection lost query-only protection"
            rows_after = memory.db.execute("SELECT * FROM memories ORDER BY id").fetchall()
            assert hashlib.sha256(json.dumps(rows_after, ensure_ascii=False).encode()).hexdigest() == logical_before, "production memory rows changed"
            assert hashlib.sha256(memory.db_path.read_bytes()).hexdigest() == database_before, "production SQLite bytes changed"
            assert memory.db.execute("PRAGMA quick_check").fetchone() == ("ok",), "production SQLite integrity check failed"
            context.send_message.assert_not_awaited()
            context.get_provider_by_id.return_value.text_chat.assert_not_awaited()
            xhs._call.assert_not_awaited()
            if hasattr(travel.http, "fetch"):
                travel.http.fetch.assert_not_awaited()
            assert candidate_manifest_digest() == manifest_before, "candidate source changed during runtime verification"
            report["checks"].append("production SQLite rows and file hash are unchanged; actual QQ, runner and provider transports remain disabled")
            report.update(ok=True, checks_count=len(report["checks"]), mocked_runner_submissions=len(runner.submits),
                          capability_names=sorted(combined), candidate_manifest_sha256=manifest_before)
    finally:
        if travel is not None:
            await travel.terminate()
        memory.db.close()
        memory.db = None
    destination = state / (os.environ.get("QQ_PARITY_MODE", "candidate") + "-route-parity.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False), flush=True)


asyncio.run(main())

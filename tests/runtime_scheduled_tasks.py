from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ["SCHEDULE_PLUGIN_ROOT"])
from codex_bridge.main import Main as Bridge
from codex_bridge.request_context import restore_event
from codex_bridge.schedules import iso, timestamp
from long_term_memory.main import Main as Memory
from reminder.main import Main as Reminder
from travel_assistant.main import Main as Travel
from xhs_search.main import Main as XHS
from image_context.main import Main as Images

CHECKS = []
SERIAL = 0


def event(text, *, sender="synthetic-schedule-owner", group="synthetic-schedule-group"):
    global SERIAL
    SERIAL += 1
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.message, message.message_str = [Plain(text)], text
    message.sender = MessageMember(sender)
    message.group_id = group
    message.self_id, message.message_id = "synthetic-bot", "synthetic-schedule-" + str(SERIAL)
    result = AstrMessageEvent(text, message, PlatformMetadata("qq_official", "schedule", "schedule"), group)
    result.is_at_or_wake_command = True
    result.send = AsyncMock(side_effect=AssertionError("Live event transport is disabled"))
    return result


def passed(label):
    CHECKS.append(label)
    print("PASS " + label, flush=True)


async def main():
    destination = Path(os.environ["SCHEDULE_REPORT"])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="schedule-framework-", dir=destination.parent) as temporary:
        root = Path(temporary)
        captured, jobs, submits = [], {}, []
        now = [timestamp("2030-01-07T12:00:00+08:00")]
        request = event("每五分钟查一次并通报，直到我停止")
        request.set_extra("codex_first_submitted", True)
        allowed = [request.unified_msg_origin]

        async def capture(umo, chain):
            assert umo == request.unified_msg_origin
            captured.append(chain)
            return True

        context = SimpleNamespace(send_message=AsyncMock(side_effect=capture), get_all_stars=lambda: [])
        bridge = Bridge(context, {"allowed_umos": allowed, "codex_first_enabled": True})
        bridge.routes_file = root / "jobs.json"
        memory = Memory(context, {"allowed_umos": allowed, "memory_file": str(root / "memory.sqlite3")})
        reminder = Reminder(context, {"allowed_umos": allowed, "reminder_file": str(root / "reminders.json")})
        travel = Travel(context, {"allowed_umos": allowed, "data_dir": str(root / "travel")})
        xhs = XHS(context, {"allowed_umos": allowed})
        images = Images(context, {"allowed_umos": allowed, "cache_dir": str(root / "images")})
        plugins = {"codex_bridge": bridge, "long_term_memory": memory, "reminder": reminder,
                   "travel_assistant": travel, "xhs_search": xhs, "image_context": images}
        context.get_all_stars = lambda: [SimpleNamespace(name=name, activated=True, star_cls=instance)
                                       for name, instance in plugins.items()]

        async def rpc(payload):
            operation, jid = payload["op"], payload.get("job_id")
            if operation == "submit":
                submits.append(copy.deepcopy(payload))
                jobs.setdefault(jid, {"id": jid, "state": "queued", "mode": "read-only", "created_at": iso(now[0]),
                    "started_at": iso(now[0]), "finished_at": None, "phase": "queued", "result": "本轮真实链路合成结果",
                    "artifacts": [], "delivery": "pending", "conversation_id": jid})
                return {"ok": True, "job": copy.deepcopy(jobs[jid])}
            if operation == "status":
                return {"ok": True, "job": copy.deepcopy(jobs[jid])} if jid in jobs else {"ok": False, "error": "job not found"}
            if operation == "delivery":
                jobs[jid]["delivery"] = payload["delivery"]
                return {"ok": True}
            if operation == "tool_pending":
                return {"ok": True, "calls": []}
            if operation == "tool_result":
                return {"ok": True}
            raise AssertionError(operation)

        bridge._rpc = AsyncMock(side_effect=rpc)
        await memory.initialize()
        try:
            with patch("codex_bridge.schedules.time.time", side_effect=lambda: now[0]):
                registry = await bridge._capability_registry(request)
                names = {tool["function"]["name"] for tool in registry.schemas(codex=True)}
                assert {"create_scheduled_task", "list_scheduled_tasks", "manage_scheduled_task", "travel_search_tickets", "xhs_read_notes"} <= names
                assert len(names) <= 32, len(names)
                for schema in registry.schemas(codex=True):
                    assert not {"owner", "umo", "sender", "event", "path"}.intersection(schema["function"]["parameters"].get("properties", {}))
                passed("real full-provider tool catalog fits the task manifest without identity selectors")

                arguments = {"title": "定时查询", "prompt": "查询已确定条件的公开信息并注明查询时间。",
                             "schedule": {"kind": "interval", "interval_minutes": 5}}
                created = await registry.execute_codex("create_scheduled_task", arguments, "create")
                assert created["ok"] and created["created"]
                task_id = created["task"]["id"]
                duplicate = await registry.execute_codex("create_scheduled_task", arguments, "same-operation")
                assert duplicate["task"]["id"] == task_id and len(bridge.schedules.records) == 1
                assert request.send.await_count == 0 and not captured
                assert bridge.routes_file.stat().st_mode & 0o777 == 0o600
                passed("real Codex-bound creation persists once without sending any QQ message")

                other = await bridge._capability_registry(event("列出我的定时任务", sender="synthetic-other"))
                assert (await other.execute_codex("list_scheduled_tasks", {}, "list"))["tasks"] == []
                assert not (await other.execute_codex("manage_scheduled_task", {"task_id": task_id, "action": "cancel"}, "cancel"))["ok"]
                passed("different sender cannot read or cancel a schedule")

                restored = Bridge(context, {"allowed_umos": allowed, "codex_first_enabled": True})
                restored.routes_file = bridge.routes_file
                restored._load()
                restored._rpc = AsyncMock(side_effect=rpc)
                plugins["codex_bridge"] = restored
                item = restored.schedules.records[task_id]
                now[0] = item["next_run_at"] + .1
                await restored.schedules.tick()
                assert len(submits) == 1
                jid = submits[0]["job_id"]
                assert submits[0]["mode"] == "read-only" and "parent_job_id" not in submits[0]
                assert "create_scheduled_task" not in {tool["name"] for tool in submits[0]["capability_tools"]}
                assert "travel_search_tickets" in {tool["name"] for tool in submits[0]["capability_tools"]}
                snapshot = restored._read_request_context(restored.routes[jid])
                restored_event = restore_event(snapshot)
                assert restored_event.get_extra("scheduled_task_id") == task_id
                passed("restart restores timers and submits an isolated read-only run with live scoped tools")

                now[0] += 700
                await restored.schedules.tick()
                await restored._poll_once()
                assert len(submits) == 1 and not captured
                jobs[jid].update(state="completed", phase="completed")
                await restored._poll_once()
                await restored.schedules.tick()
                assert len(captured) == 1
                assert "本轮真实链路合成结果" in captured[0].chain[0].text
                assert "/定时任务停止 " + task_id in captured[0].chain[0].text
                assert item["last_run"]["delivery"] == "delivered" and item["next_run_at"] > now[0]
                passed("no overlap or progress spam; final result captured for the original chat only")

                now[0] = item["next_run_at"] + .1
                await restored.schedules.tick()
                running = item["inflight"]["job_id"]
                controller = await restored._capability_registry(event("别继续了"))
                cancelled = await controller.execute_codex("manage_scheduled_task", {"task_id": task_id, "action": "cancel"}, "stop")
                assert cancelled["ok"]
                jobs[running].update(state="completed", result="must never be sent")
                await restored._poll_once()
                await restored.schedules.tick()
                assert len(captured) == 1 and item["state"] == "cancelled"
                now[0] += 10000
                await restored.schedules.tick()
                assert len(submits) == 2
                passed("Codex stop suppresses the in-flight result and prevents further runs")

                handlers = {handler.handler_name: handler for handler in star_handlers_registry._handlers
                            if handler.handler_module_path == "codex_bridge.main"}
                assert {"list_scheduled_tasks", "stop_scheduled_task"} <= set(handlers)
                assert handlers["stop_scheduled_task"].extras_configs["priority"] == 100
                passed("model-independent list and stop commands are registered in the real framework")

                disabled = Bridge(context, {"allowed_umos": allowed, "scheduled_tasks_enabled": False})
                disabled.routes_file = root / "disabled.json"
                disabled_registry = await disabled._capability_registry(event("设置定时任务"))
                assert "create_scheduled_task" not in {tool["function"]["name"] for tool in disabled_registry.schemas(codex=True)}
                passed("administrator disable switch removes scheduling tools")

            report = {"ok": True, "count": len(CHECKS), "checks": CHECKS, "qq_messages_sent": 0,
                      "captured_synthetic_deliveries": len(captured), "production_mutations": 0, "catalog_count": len(names)}
            destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            destination.chmod(0o600)
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            await memory.terminate()
            await bridge.terminate()
            if "restored" in locals():
                await restored.terminate()


if __name__ == "__main__":
    asyncio.run(main())

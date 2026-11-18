"""Real timer/submission/delivery logic with synthetic transport and clock."""
import asyncio
import copy
import importlib
import json
import stat
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_plugin import Event, UMO, job, plugin

scheduling = importlib.import_module(plugin.__package__ + ".schedules")
requests = importlib.import_module(plugin.__package__ + ".request_context")
NOW = scheduling.timestamp("2030-01-07T12:00:00+08:00")


class ScheduleRuleTests(unittest.TestCase):
    def test_beijing_and_explicit_timezone_are_equal(self):
        self.assertEqual(scheduling.timestamp("2030-01-07 12:00"), NOW)
        self.assertEqual(scheduling.timestamp("2030-01-07T04:00:00Z"), NOW)
        self.assertEqual(scheduling.iso(NOW), "2030-01-07T12:00:00+08:00")

    def test_once_interval_daily_weekly_and_missed_slots(self):
        rules = [({"kind": "once", "at": "2030-01-07T13:00:00+08:00"}, 3600),
                 ({"kind": "interval", "interval_minutes": 5}, 300),
                 ({"kind": "daily", "time": "12:30"}, 1800),
                 ({"kind": "daily", "time": "08:00"}, 20 * 3600),
                 ({"kind": "weekly", "time": "12:00", "weekdays": [1]}, 7 * 86400),
                 ({"kind": "weekly", "time": "08:00", "weekdays": [3, 5]}, 44 * 3600)]
        for rule, seconds in rules:
            with self.subTest(rule=rule):
                self.assertEqual(scheduling.next_time(scheduling.validate_schedule(rule), NOW), NOW + seconds)
        self.assertEqual(scheduling.next_time({"kind": "interval", "interval_minutes": 5}, NOW + 1040, previous=NOW), NOW + 1200)
        self.assertIsNone(scheduling.next_time(rules[0][0], NOW + 4000, previous=NOW + 3600))

    def test_bad_or_mixed_rules_fail(self):
        invalid = [{}, {"kind": "cron", "expression": "* * * * *"}, {"kind": "once", "at": "明天"},
                   {"kind": "interval", "interval_minutes": 4}, {"kind": "interval", "interval_minutes": True},
                   {"kind": "daily", "time": "25:01"}, {"kind": "daily", "time": "08:00", "at": "2030-01-08T12:00"},
                   {"kind": "weekly", "time": "08:00", "weekdays": [0]}, {"kind": "weekly", "time": "08:00", "weekdays": [True]}]
        for rule in invalid:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                scheduling.validate_schedule(rule)


class ScheduledTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = NOW
        self.clock = patch.object(scheduling.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True), get_all_stars=lambda: [])
        self.bridge = self.new_bridge()
        self.events = []
        self.jobs, self.submissions, self.results = {}, [], []
        self.bridge._rpc = AsyncMock(side_effect=self.rpc)
        self.serial = 0

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def new_bridge(self):
        bridge = plugin.Main(self.context, {"allowed_umos": [UMO, "qq:GroupMessage:other"], "codex_first_enabled": True})
        bridge.routes_file = self.root / "jobs.json"
        return bridge

    async def rpc(self, payload):
        operation = payload["op"]
        if operation == "submit":
            self.submissions.append(copy.deepcopy(payload))
            current = self.jobs.setdefault(payload["job_id"], job("queued", id=payload["job_id"], created_at=scheduling.iso(self.now),
                                                                  artifacts=[], result="本轮重新查询的结果", conversation_id=payload["job_id"]))
            return {"ok": True, "job": copy.deepcopy(current)}
        if operation == "status":
            current = self.jobs.get(payload["job_id"])
            return {"ok": True, "job": copy.deepcopy(current)} if current else {"ok": False, "error": "job not found"}
        if operation == "delivery":
            self.jobs[payload["job_id"]]["delivery"] = payload["delivery"]
            return {"ok": True}
        if operation == "tool_pending":
            return {"ok": True, "calls": []}
        if operation == "tool_result":
            self.results.append(payload)
            return {"ok": True}
        raise AssertionError(operation)

    async def create(self, schedule=None, *, event=None, **keywords):
        self.serial += 1
        event = event or Event("每5分钟查询并通报，直到停止", message_id="schedule-" + str(self.serial))
        self.events.append(event)
        return await self.bridge.schedules.create(event, keywords.pop("title", "公开信息定时查询"),
            keywords.pop("prompt", "重新查询指定公开信息并注明查询时间，不使用缓存冒充新结果。"),
            schedule or {"kind": "interval", "interval_minutes": 5}, **keywords)

    def item(self, result):
        self.assertTrue(result["ok"], result)
        return self.bridge.schedules.records[result["task"]["id"]]

    async def due(self, item):
        self.now = item["next_run_at"] + .1
        await self.bridge.schedules.tick()
        return item["inflight"]["job_id"]

    async def finish(self, jid, *, state="completed"):
        self.jobs[jid].update(state=state, phase=state)
        await self.bridge._poll_once()
        await self.bridge.schedules.tick()

    async def test_create_is_durable_scoped_private_and_has_no_qq_side_effect(self):
        result = await self.create()
        self.assertEqual(result["task"]["next_run_at"], scheduling.iso(NOW + 300))
        self.assertEqual(stat.S_IMODE(self.bridge.routes_file.stat().st_mode), 0o600)
        restored = self.new_bridge()
        restored._load()
        self.assertEqual(restored.schedules.records, self.bridge.schedules.records)
        self.context.send_message.assert_not_awaited()
        self.assertFalse(self.events[0].sent)

    async def test_same_original_message_is_idempotent_even_after_first_due(self):
        event = Event("定时运行", message_id="stable-message")
        first = await self.create(event=event)
        self.now += 1000
        second = await self.create(event=event)
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.bridge.schedules.records), 1)

    async def test_validation_quota_owner_and_storage_failure(self):
        denied = await self.create(event=Event("定时运行", umo="other"))
        self.assertFalse(denied["ok"])
        self.assertFalse((await self.create({"kind": "once", "at": scheduling.iso(self.now - 1)}))["ok"])
        self.assertFalse((await self.create(end_at=scheduling.iso(self.now + 100)))["ok"])
        for index in range(5):
            self.assertTrue((await self.create(title="任务" + str(index)))["ok"])
        self.assertEqual((await self.create())["code"], "schedule_quota")
        listed = await self.bridge.schedules.list_tasks(Event(sender="bob"))
        self.assertEqual(listed["tasks"], [])
        with patch.object(self.bridge, "_save", side_effect=OSError("private failure")):
            result = await self.create(event=Event(sender="bob", message_id="another"))
        self.assertEqual(result["code"], "schedule_storage_unavailable")
        self.assertEqual(len(self.bridge.schedules.records), 5)

    async def test_timer_submits_normal_readonly_job_and_no_progress_spam(self):
        item = self.item(await self.create())
        await self.bridge.schedules.tick()
        self.assertEqual(self.submissions, [])
        jid = await self.due(item)
        self.assertEqual(len(self.submissions), 1)
        payload = self.submissions[0]
        self.assertEqual(payload["owner"], item["owner"])
        self.assertEqual(payload["mode"], "read-only")
        self.assertIn("本轮只执行一次", payload["prompt"])
        self.assertIn(item["prompt"], payload["prompt"])
        self.assertNotIn("parent_job_id", payload)
        self.assertNotIn(jid, self.bridge._events)
        self.now += 150
        await self.bridge._poll_once()
        self.context.send_message.assert_not_awaited()

    async def test_actual_result_goes_to_original_chat_then_next_slot_without_overlap(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        self.now += 740
        await self.bridge.schedules.tick()
        self.assertEqual(len(self.submissions), 1)
        await self.finish(jid)
        self.assertEqual(item["run_count"], 1)
        self.assertEqual(item["last_run"]["delivery"], "delivered")
        self.assertIsNone(item["inflight"])
        self.assertEqual(item["next_run_at"], NOW + 1200)
        destination, chain = self.context.send_message.call_args.args
        self.assertEqual(destination, UMO)
        text = chain.chain[0].text
        self.assertIn("本轮重新查询的结果", text)
        self.assertIn("/定时任务停止 " + item["id"], text)
        self.assertNotIn("已受理", text)

    async def test_restart_resumes_inflight_without_duplicate_submission(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        restored = self.new_bridge()
        restored._load()
        restored._rpc = AsyncMock(side_effect=self.rpc)
        self.bridge = restored
        await restored.schedules.tick()
        self.assertEqual(len(self.submissions), 1)
        await self.finish(jid)
        self.assertEqual(restored.schedules.records[item["id"]]["run_count"], 1)
        self.context.send_message.assert_awaited_once()

    async def test_lost_submit_ack_recovers_before_rebuilding_changed_context(self):
        item = self.item(await self.create())
        attempts = 0
        async def uncertain(payload):
            nonlocal attempts
            result = await self.rpc(payload)
            if payload["op"] == "submit":
                attempts += 1
                if attempts == 1:
                    raise OSError("reply lost after accept")
            return result
        self.bridge._rpc = AsyncMock(side_effect=uncertain)
        jid = await self.due(item)
        self.now += 31
        await self.bridge.schedules.tick()
        self.assertEqual([value["job_id"] for value in self.submissions], [jid])
        self.assertEqual(len(self.jobs), 1)
        self.assertTrue(self.bridge.routes[jid]["schedule_submitted"])

    async def test_pause_cancel_block_scheduled_results_and_new_tools(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        foreign = await self.bridge.schedules.manage(Event(sender="bob"), item["id"], "cancel")
        self.assertEqual(foreign["code"], "schedule_not_found")
        result = await self.bridge.schedules.manage(Event(), item["id"], "cancel")
        self.assertTrue(result["ok"])
        registry = self.bridge._registries[jid]
        registry.execute_codex = AsyncMock()
        await self.bridge._execute_tool_call(self.bridge.routes[jid], registry, "a" * 32, "list_my_long_term_memories", {})
        registry.execute_codex.assert_not_awaited()
        self.assertEqual(self.results[-1]["result"]["code"], "schedule_inactive")
        await self.finish(jid)
        self.context.send_message.assert_not_awaited()
        self.assertEqual(item["last_run"]["delivery"], "suppressed")
        self.now += 10000
        await self.bridge.schedules.tick()
        self.assertEqual(len(self.submissions), 1)

    async def test_pause_resume_keeps_old_run_suppressed(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        await self.bridge.schedules.manage(Event(), item["id"], "pause")
        await self.bridge.schedules.manage(Event(), item["id"], "resume")
        self.assertFalse(self.bridge.schedules.permits(self.bridge.routes[jid]))
        await self.finish(jid)
        self.context.send_message.assert_not_awaited()
        self.assertEqual(item["state"], "active")
        self.assertGreater(item["next_run_at"], self.now)
        second = await self.due(item)
        self.assertNotEqual(second, jid)

    async def test_pause_during_unaccepted_submission_does_not_wedge_resume(self):
        item = self.item(await self.create())
        self.bridge._rpc = AsyncMock(side_effect=OSError("connection refused"))
        jid = await self.due(item)
        await self.bridge.schedules.manage(Event(), item["id"], "pause")
        await self.bridge.schedules.manage(Event(), item["id"], "resume")
        self.bridge._rpc = AsyncMock(side_effect=self.rpc)
        await self.bridge._poll_once()
        await self.bridge.schedules.tick()
        self.assertTrue(self.bridge.routes[jid]["acked"])
        self.assertIsNone(item["inflight"])
        self.assertGreater(item["next_run_at"], self.now)

    async def test_stop_and_list_commands_work_without_model(self):
        item = self.item(await self.create())
        event = Event("/定时任务列表")
        await self.bridge.list_scheduled_tasks(event)
        self.assertIn(item["id"], event.sent[-1])
        await self.bridge.stop_scheduled_task(event, item["id"])
        self.assertEqual(item["state"], "cancelled")
        self.assertIn("已停止", event.sent[-1])
        self.assertTrue(event.stopped)

    async def test_once_completes_and_end_time_suppresses_late_result(self):
        once = self.item(await self.create({"kind": "once", "at": scheduling.iso(self.now + 10)}))
        jid = await self.due(once)
        await self.finish(jid)
        self.assertEqual(once["state"], "completed")
        repeating = self.item(await self.create(end_at=scheduling.iso(self.now + 350)))
        jid = await self.due(repeating)
        self.now += 60
        self.context.send_message.reset_mock()
        await self.finish(jid)
        self.context.send_message.assert_not_awaited()
        await self.bridge.schedules.tick()
        self.assertEqual(repeating["state"], "completed")

    async def test_delivery_failure_pauses_instead_of_spamming_forever(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        self.context.send_message.return_value = False
        self.jobs[jid]["state"] = "completed"
        for attempt in range(6):
            self.now += 301
            await self.bridge._poll_once()
        await self.bridge.schedules.tick()
        self.assertEqual(self.context.send_message.await_count, 5)
        self.assertEqual(item["state"], "paused")
        self.assertEqual(item["last_run"]["delivery"], "failed")

    async def test_disabled_or_revoked_schedule_does_not_run_or_deliver(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        self.bridge.config["scheduled_tasks_enabled"] = False
        await self.finish(jid)
        self.context.send_message.assert_not_awaited()
        self.bridge.config["scheduled_tasks_enabled"] = True
        self.bridge.config["allowed_umos"] = []
        self.now += 5000
        await self.bridge.schedules.tick()
        self.assertEqual(len(self.submissions), 1)

    async def test_storage_corruption_fails_closed_and_old_routes_load(self):
        item = self.item(await self.create())
        state = json.loads(self.bridge.routes_file.read_text())
        state["scheduled_tasks"][0]["request"]["sender"] = "different-person"
        self.bridge.routes_file.write_text(json.dumps(state))
        restored = self.new_bridge()
        restored._load()
        self.assertFalse(restored._storage_ok)
        self.assertEqual(restored.schedules.records, {})
        self.bridge.routes_file.write_text(json.dumps({"version": 1, "jobs": []}))
        restored = self.new_bridge()
        restored._load()
        self.assertTrue(restored._storage_ok)

    async def test_real_catalog_create_manage_and_restored_timer_cannot_recurse(self):
        event = Event("从聊天理解任务", message_id="catalog")
        event.set_extra("codex_first_submitted", True)
        registry = await self.bridge._capability_registry(event)
        names = {tool["function"]["name"] for tool in registry.schemas(codex=True)}
        self.assertTrue({"create_scheduled_task", "list_scheduled_tasks", "manage_scheduled_task"} <= names)
        result = await registry.execute_codex("create_scheduled_task", {"title": "测试", "prompt": "查询已确定信息",
            "schedule": {"kind": "interval", "interval_minutes": 5}}, "create")
        item = self.item(result)
        jid = await self.due(item)
        restored = requests.restore_event(self.bridge._read_request_context(self.bridge.routes[jid]))
        self.assertEqual(restored.get_extra("scheduled_task_id"), item["id"])
        timer_registry = await self.bridge._capability_registry(restored)
        self.assertNotIn("create_scheduled_task", {tool["function"]["name"] for tool in timer_registry.schemas(codex=True)})
        self.assertFalse((await self.bridge.schedules.create(restored, "重复", "重复", {"kind": "interval", "interval_minutes": 5}))["ok"])

    async def test_failed_run_does_not_expose_raw_error(self):
        item = self.item(await self.create())
        jid = await self.due(item)
        self.jobs[jid]["error"] = "/private/secret failure"
        await self.finish(jid, state="failed")
        text = self.context.send_message.call_args.args[1].chain[0].text
        self.assertIn("本轮任务执行失败", text)
        self.assertNotIn("/private", text)

    async def test_pause_resume_pause_obeys_latest_action_not_stale_receipt(self):
        item = self.item(await self.create())
        registry = await self.bridge._capability_registry(Event("调整定时任务"))
        for index, action in enumerate(("pause", "resume", "pause")):
            result = await registry.execute_codex("manage_scheduled_task", {"task_id": item["id"], "action": action}, "change-" + str(index))
            self.assertTrue(result["ok"])
        self.assertEqual(item["state"], "paused")


if __name__ == "__main__":
    unittest.main()

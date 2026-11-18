from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from memory_tools import (MAX_DISPLAY_CONTENT, MAX_PAYLOAD, TASK_MEMORY_PATH, query_snapshot, read_snapshot,
                          unavailable, validate_snapshot, write_snapshot)

spec = importlib.util.spec_from_file_location("runner_under_test", ROOT / "codex-runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def snapshot(count=2, **overrides):
    value = {"status": "ok", "observed_at": "2026-11-03T14:00:00+08:00",
             "items": [{"id": i + 1, "content": f"Saved fact {i + 1}"} for i in range(count)],
             "total": count}
    return {**value, **overrides}


class SnapshotTests(unittest.TestCase):
    def test_optional_display_content_survives_copy_and_pagination(self):
        source = snapshot(6)
        for item in source["items"]:
            item["display_content"] = f"展示事项 {item['id']}"
        original = copy.deepcopy(source)
        copied = validate_snapshot(source)
        self.assertEqual(copied, original)
        first, second = (query_snapshot(source, page=page) for page in (1, 2))
        self.assertEqual(first["items"] + second["items"], original["items"])
        self.assertEqual((first["total"], second["total"]), (6, 6))
        copied["items"][0]["display_content"] = "modified display"
        first["items"][0]["content"] = "modified source"
        self.assertEqual(source, original)

    def test_display_validation_keeps_strict_record_shape_and_boundaries(self):
        valid = snapshot(1, items=[{"id": 1, "content": "original <@account>",
                                    "display_content": "显" * MAX_DISPLAY_CONTENT}])
        self.assertEqual(validate_snapshot(valid), valid)
        for display in ("", "   ", None, 1, True, [], "x" * (MAX_DISPLAY_CONTENT + 1)):
            with self.subTest(display=repr(display)[:40]):
                invalid = snapshot(1, items=[{"id": 1, "content": "original", "display_content": display}])
                with self.assertRaises(ValueError):
                    validate_snapshot(invalid)
        for extra in ("owner", "path", "umo", "sender", "session", "operation"):
            for record in ({"id": 1, "content": "original", extra: "other"},
                           {"id": 1, "content": "original", "display_content": "展示", extra: "other"}):
                with self.subTest(extra=extra, record=record):
                    with self.assertRaises(ValueError):
                        validate_snapshot(snapshot(1, items=[record]))
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot(1, items=[{"id": 1, "display_content": "展示不能替代原文"}]))

    def test_display_counts_toward_total_payload_limit(self):
        source = snapshot(50)
        for item in source["items"]:
            item["display_content"] = "显" * MAX_DISPLAY_CONTENT
        with self.assertRaisesRegex(ValueError, "payload too large"):
            validate_snapshot(source)

    def test_search_matches_original_or_display_without_replacing_content(self):
        source = snapshot(3, items=[
            {"id": 1, "content": "原始关系 <@account_1>", "display_content": "QQ 用户：First   Person"},
            {"id": 2, "content": "legacy FACT"},
            {"id": 3, "content": "another fact", "display_content": "展示独有词"},
        ])
        original = copy.deepcopy(source)
        for needle, index in (("account_1", 0), ("FIRST person", 0), ("legacy fact", 1), ("独有词", 2)):
            with self.subTest(needle=needle):
                result = query_snapshot(source, query=needle)
                self.assertEqual(result["items"], [original["items"][index]])
                self.assertEqual((result["total"], result["matched_total"]), (3, 1))
        self.assertEqual(source, original)

    def test_display_is_preserved_in_private_snapshot_file(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            source = snapshot(2)
            source["items"][0]["display_content"] = "QQ 用户（标识：account）"
            path = write_snapshot(Path(raw) / "snapshots", "d" * 16, source)
            self.assertEqual(read_snapshot(path), source)

    def test_snapshot_validation_copies_data(self):
        source = snapshot()
        result = validate_snapshot(source)
        result["items"][0]["content"] = "changed"
        self.assertEqual(source["items"][0]["content"], "Saved fact 1")

    def test_unavailable_and_empty_are_distinct(self):
        missing = query_snapshot(validate_snapshot(None))
        empty = query_snapshot(snapshot(0))
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["status"], "unavailable")
        self.assertIsNone(missing["total"])
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["total"], 0)

    def test_unavailable_error_survives(self):
        for code in ("plugin_unavailable", "read_failed", "not_authorized", "too_many_records"):
            with self.subTest(code=code):
                result = query_snapshot(unavailable(code))
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], code)
                self.assertIsNone(result["total"])

    def test_limits_and_no_silent_truncation(self):
        self.assertEqual(validate_snapshot(snapshot(200))["total"], 200)
        invalid = [snapshot(201), snapshot(1, items=[{"id": 1, "content": "x" * 1001}]),
                   snapshot(1, total=0), snapshot(1, items=[{"id": True, "content": "x"}]),
                   snapshot(2, items=[{"id": 1, "content": "a"}, {"id": 1, "content": "b"}])]
        for source in invalid:
            with self.subTest(source=repr(source)[:80]):
                with self.assertRaises(ValueError):
                    validate_snapshot(source)

    def test_payload_limit_uses_bytes(self):
        source = snapshot(200)
        for item in source["items"]:
            item["content"] = "记" * 1000
        with self.assertRaisesRegex(ValueError, "payload too large"):
            validate_snapshot(source)

    def test_rejects_scope_arguments_and_naive_time(self):
        for source in (snapshot(owner="other"), snapshot(path="/some/db"),
                       snapshot(observed_at="2026-11-03T14:00:00"),
                       {**unavailable(), "items": [{"id": 1, "content": "partial"}]}):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    validate_snapshot(source)

    def test_pagination_returns_every_record(self):
        source = snapshot(12)
        first, second, third = [query_snapshot(source, page=i) for i in (1, 2, 3)]
        self.assertEqual([first["pages"], second["pages"], third["pages"]], [3, 3, 3])
        self.assertEqual([item["id"] for page in (first, second, third) for item in page["items"]], list(range(1, 13)))
        self.assertEqual(query_snapshot(source, page=4)["code"], "page_out_of_range")

    def test_search_is_literal_and_bounded(self):
        source = snapshot(3, items=[{"id": 1, "content": "Tea   with MILK"},
                                    {"id": 2, "content": "tea without milk"},
                                    {"id": 3, "content": ".* special"}])
        result = query_snapshot(source, query="tea with milk")
        self.assertEqual(result["items"], [source["items"][0]])
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["matched_total"], 1)
        self.assertEqual(query_snapshot(source, query=".*")["matched_total"], 1)
        for query in ("", "x" * 201):
            self.assertEqual(query_snapshot(source, query=query)["code"], "invalid_query")
        for page in (0, -1, True, "1"):
            self.assertEqual(query_snapshot(source, page=page)["code"], "invalid_page")

    def test_snapshot_is_private_and_literal(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            directory = Path(raw) / "private"
            victim = Path(raw) / "executed"
            content = f'__import__("pathlib").Path({str(victim)!r}).touch()'
            source = snapshot(1, items=[{"id": 1, "content": content}])
            path = write_snapshot(directory, "1" * 16, source)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            result = query_snapshot(read_snapshot(path))
            self.assertEqual(result["items"][0]["content"], content)
            self.assertFalse(victim.exists())
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_snapshot(directory, "1" * 16, source)

    def test_unreadable_snapshot_cannot_claim_empty(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            directory = Path(raw)
            original = directory / "source"
            original.write_text(json.dumps(snapshot(0)))
            link = directory / "link"
            link.symlink_to(original)
            self.assertEqual(read_snapshot(link)["status"], "unavailable")
            self.assertEqual(read_snapshot(directory / "absent")["code"], "read_failed")
            original.write_text("x" * (MAX_PAYLOAD + 1))
            self.assertEqual(read_snapshot(original)["status"], "unavailable")

    def test_worker_command_binds_one_snapshot_readonly(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            workspace = Path(raw) / "workspace"
            workspace.mkdir()
            memory = write_snapshot(Path(raw) / "private", "1" * 16, snapshot())
            args = runner.command(workspace, "workspace-write", memory_path=memory)
            at = args.index(str(TASK_MEMORY_PATH))
            self.assertEqual(args[at - 2:at + 1], ["--ro-bind", str(memory), str(TASK_MEMORY_PATH)])
            self.assertLess(args.index("/run/astrbot-codex"), at)
            self.assertNotIn("/run", args)
            configs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"]
            self.assertTrue(any(value.startswith("mcp_servers.long_term_memory.args=") for value in configs))
            self.assertTrue(any("memory_mcp.py" in value for value in configs))
            self.assertFalse(any("owner" in value or str(memory) in value for value in configs))

    def test_default_command_preserves_previous_interface(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            args = runner.command(Path(raw), "read-only")
            self.assertFalse(any("long_term_memory" in value for value in args))
            self.assertIn("/run/astrbot-codex", args)

    def test_resume_subcommand_receives_task_memory_configuration(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as raw:
            base = Path(raw)
            workspace, home = base / "workspace", base / "home"
            workspace.mkdir()
            home.mkdir()
            memory = write_snapshot(base / "private", "1" * 16, snapshot())
            args = runner.command(workspace, "read-only", session_home=home,
                                  resume_thread_id="11111111-1111-4111-8111-111111111111", memory_path=memory)
            resume = args.index("resume")
            configs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"
                       and args[i + 1].startswith("mcp_servers.long_term_memory.")]
            resumed_configs = [args[i + 1] for i in range(resume + 1, len(args) - 1)
                               if args[i] == "-c" and args[i + 1].startswith("mcp_servers.long_term_memory.")]
            self.assertEqual(len(resumed_configs), 4)
            self.assertEqual(configs, resumed_configs * 2)
            self.assertEqual({value.split("=", 1)[0] for value in resumed_configs},
                             {"mcp_servers.long_term_memory.command", "mcp_servers.long_term_memory.args",
                              "mcp_servers.long_term_memory.startup_timeout_sec", "mcp_servers.long_term_memory.tool_timeout_sec"})
            at = args.index(str(TASK_MEMORY_PATH))
            self.assertEqual(args[at - 2:at + 1], ["--ro-bind", str(memory), str(TASK_MEMORY_PATH)])


class AliveTask:
    def done(self):
        return False


class RunnerSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.base = Path(self.temporary.name)
        self.root_patch = patch.object(runner, "ROOT", self.base / "workspaces")
        self.root_patch.start()
        self.manager = runner.JobManager(self.base / "jobs")
        self.manager.worker = self.manager.heartbeat_task = AliveTask()

    def tearDown(self):
        self.root_patch.stop()
        self.temporary.cleanup()

    def submit(self, identity="1", owner="a", data=None):
        return self.manager.request({"op": "submit", "job_id": identity * 16,
                                     "owner": owner * 64, "workspace": "b" * 32,
                                     "prompt": "Tell me my saved memories", "memory_snapshot": data or snapshot()})

    def test_scope_is_bound_to_rpc_owner_and_not_persisted(self):
        self.submit()
        private = self.manager.jobs["1" * 16]
        self.assertEqual(private["_owner"], "a" * 64)
        self.assertEqual(private["_memory_snapshot"]["total"], 2)
        record = json.loads((self.base / "jobs" / ("1" * 16 + ".json")).read_text())
        self.assertNotIn("_memory_snapshot", record)
        self.assertNotIn("Saved fact", json.dumps(record))
        self.assertNotIn("memory_snapshot", self.submit()["job"])
        with self.assertRaisesRegex(ValueError, "job not found"):
            self.manager.request({"op": "status", "owner": "c" * 64, "job_id": "1" * 16})

    def test_duplicate_submit_tolerates_timestamp_only_retry(self):
        self.submit()
        retry = snapshot(observed_at="2026-11-03T14:02:00+08:00")
        self.assertTrue(self.submit(data=retry)["ok"])
        changed = snapshot(1)
        with self.assertRaisesRegex(ValueError, "collision"):
            self.submit(data=changed)

    def test_display_payload_is_scoped_copied_and_bound_to_retry(self):
        source = snapshot(1, items=[{"id": 1, "content": "原始事实 <@account>",
                                    "display_content": "QQ 用户（标识：account）的事实"}])
        original = copy.deepcopy(source)
        self.submit(data=source)
        job = self.manager.jobs["1" * 16]
        self.assertEqual(job["_memory_snapshot"], original)
        self.assertEqual(job["_owner"], "a" * 64)
        source["items"][0]["display_content"] = "a later display"
        self.assertEqual(job["_memory_snapshot"], original)
        with self.assertRaisesRegex(ValueError, "collision"):
            self.submit(data=source)
        disk = json.loads((self.base / "jobs" / ("1" * 16 + ".json")).read_text())
        self.assertNotIn("display_content", json.dumps(disk))

    async def exercise_worker(self, fail=False):
        observed = []

        async def fake_run(prompt, mode, workspace, progress, **kwargs):
            path = kwargs["memory_path"]
            self.assertNotIn(workspace, path.parents)
            self.assertEqual(path.parent, self.base / "jobs" / "memory-snapshots")
            observed.append(read_snapshot(path))
            if fail:
                raise ValueError("test execution failure")
            return "Saved memory list read successfully"

        self.submit()
        other = snapshot(1, items=[{"id": 8, "content": "Other user"}])
        self.submit(identity="2", owner="c", data=other)
        with patch.object(runner, "run_codex", fake_run):
            task = asyncio.create_task(self.manager.work())
            try:
                await asyncio.wait_for(self.manager.queue.join(), timeout=5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual([value["total"] for value in observed], [2, 1])
        self.assertEqual(observed[1]["items"][0]["content"], "Other user")
        self.assertEqual(list((self.base / "jobs" / "memory-snapshots").glob("*.json")), [])
        self.assertTrue(all("_memory_snapshot" not in job for job in self.manager.jobs.values()))
        self.assertEqual({job["state"] for job in self.manager.jobs.values()}, {"failed" if fail else "completed"})

    async def test_worker_keeps_owners_separate_and_cleans_success(self):
        await self.exercise_worker()

    async def test_worker_cleans_failed_snapshots(self):
        await self.exercise_worker(fail=True)

    async def test_cancellation_removes_task_snapshot(self):
        started = asyncio.Event()

        async def blocked_run(*args, **kwargs):
            self.assertEqual(read_snapshot(kwargs["memory_path"])["total"], 2)
            started.set()
            await asyncio.Event().wait()

        self.submit()
        with patch.object(runner, "run_codex", blocked_run):
            task = asyncio.create_task(self.manager.work())
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(list((self.base / "jobs" / "memory-snapshots").glob("*.json")), [])
        self.assertEqual(self.manager.jobs["1" * 16]["state"], "interrupted")

    def test_restart_removes_stale_snapshot(self):
        path = write_snapshot(self.base / "jobs" / "memory-snapshots", "f" * 16, snapshot())
        runner.JobManager(self.base / "jobs")
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

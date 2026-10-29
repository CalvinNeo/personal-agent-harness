"""No network/model calls: exercise real Unix RPC and local JSON-emitting processes."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("codex_runner", Path(__file__).with_name("codex-runner.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
OWNER = "a" * 64
OTHER = "b" * 64
WORKSPACE = "c" * 32


def request(job_id="1111111111111111", **kwargs):
    return dict(op="submit", owner=OWNER, workspace=WORKSPACE, prompt="test", mode="read-only", job_id=job_id, **kwargs)


def fake_program(*body):
    return "import json,sys,time,os\nsys.stdin.read()\n" + "\n".join(body)


def emit(event):
    return "print(" + repr(json.dumps(event)) + ",flush=True)"


SUCCESS = fake_program(
    emit({"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"}),
    emit({"type": "turn.started"}),
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Final result"}}),
    emit({"type": "turn.completed"}),
)


class CommandSecurityTests(unittest.TestCase):
    def test_required_wrapper_and_mount_isolation(self):
        workspace = Path("/var/lib/codexrunner/workspaces/" + WORKSPACE)
        for mode in ("read-only", "workspace-write"):
            cmd = r.command(workspace, mode)
            expected = [
                "/opt/astrbot-codex-runner/codex-bwrap",
                "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid",
                "--unshare-ipc", "--unshare-uts", "--ro-bind", "/", "/",
                "--proc", "/proc",
                "--ro-bind", "/dev/null", "/proc/kcore",
                "--ro-bind", "/dev/null", "/proc/kmsg",
                "--ro-bind", "/dev/null", "/proc/keys",
                "--ro-bind", "/dev/null", "/proc/timer_list",
                "--ro-bind", "/dev/null", "/proc/sysrq-trigger",
                "--ro-bind", "/proc/sys", "/proc/sys",
                "--dev", "/dev", "--tmpfs", "/tmp",
                "--tmpfs", "/var/lib/codexrunner", "--dir", "/var/lib/codexrunner/codex",
                "--ro-bind", "/var/lib/codexrunner/codex/config.toml", "/var/lib/codexrunner/codex/config.toml",
                "--dir", "/var/lib/codexrunner/workspaces",
                "--bind" if mode == "workspace-write" else "--ro-bind", str(workspace), str(workspace),
                "--bind", str(workspace / ".tmp"), str(workspace / ".tmp"),
                "--tmpfs", "/run/astrbot-codex", "--chdir", str(workspace), "--",
                "/usr/local/bin/codex", "exec", "--ephemeral", "--json",
                "--disable", "plugins", "--disable", "apps", "--skip-git-repo-check",
                "--sandbox", mode, "-C", str(workspace), "-m", r.MODEL, "-",
            ]
            self.assertEqual(cmd, expected)
        with self.assertRaisesRegex(ValueError, "invalid mode"):
            r.command(workspace, "danger-full-access")


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(r, "ROOT", self.root / "workspaces"), patch.object(r, "TIMEOUT", 2), patch.object(r, "HEARTBEAT", .03)]
        for p in self.patches:
            p.start()
        self.program = SUCCESS
        self.command_patch = patch.object(r, "command", lambda workspace, mode, image_paths=None, **kwargs: [sys.executable, "-u", "-c", self.program.replace("sys.stdin.read()", "sys.stdin.read(); print(json.dumps({\"type\":\"thread.started\",\"thread_id\":\"11111111-1111-4111-8111-111111111111\"}),flush=True)")])
        self.command_patch.start()
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.command_patch.stop()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def finished(self, job_id="1111111111111111"):
        async with asyncio.timeout(4):
            while self.manager.jobs[job_id]["state"] in r.ACTIVE:
                await asyncio.sleep(.01)
        return self.manager.jobs[job_id]

    async def test_immediate_submit_then_final_result_and_private_disk(self):
        self.program = fake_program("time.sleep(.15)", emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}))
        started = time.monotonic()
        response = self.manager.request(request())
        self.assertLess(time.monotonic() - started, .12)
        self.assertEqual(response["job"]["state"], "queued")
        self.assertNotIn("_owner", response["job"])
        path = self.root / "jobs" / "1111111111111111.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("_prompt", json.loads(path.read_text()))
        self.assertEqual((await self.finished())["result"], "done")

    async def test_socket_disconnect_does_not_cancel_job(self):
        socket_path = str(self.root / "test.sock")
        self.program = fake_program("time.sleep(.1)", emit({"type": "item.completed", "item": {"type": "agent_message", "text": "survived"}}))
        with patch.object(r, "peer_uid", return_value=next(iter(r.ALLOWED_UIDS))):
            server = await asyncio.start_unix_server(lambda reader, writer: r.handle(reader, writer, self.manager), path=socket_path)
            async with server:
                _, writer = await asyncio.open_unix_connection(socket_path)
                writer.write(json.dumps(request()).encode() + b"\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                async with asyncio.timeout(1):
                    while not self.manager.jobs:
                        await asyncio.sleep(.01)
                job = await self.finished()
                self.assertEqual(job["state"], "completed")
                self.assertEqual(job["result"], "survived")

    async def test_rejected_peer_never_submits(self):
        socket_path = str(self.root / "test.sock")
        with patch.object(r, "peer_uid", return_value=1234):
            server = await asyncio.start_unix_server(lambda reader, writer: r.handle(reader, writer, self.manager), path=socket_path)
            async with server:
                reader, writer = await asyncio.open_unix_connection(socket_path)
                result = json.loads(await reader.readline())
                self.assertFalse(result["ok"])
                self.assertFalse(self.manager.jobs)
                writer.close()
                await writer.wait_closed()

    async def test_owner_isolation_and_delivery(self):
        self.manager.request(request())
        await self.finished()
        for op in ("status", "delivery"):
            with self.assertRaisesRegex(ValueError, "not found"):
                self.manager.request(dict(op=op, owner=OTHER, job_id="11111111", delivery="delivered"))
        self.assertEqual(self.manager.request(dict(op="list", owner=OTHER))["jobs"], [])
        result = self.manager.request(dict(op="status", owner=OWNER, job_id="11111111"))
        self.assertEqual(result["job"]["result"], "Final result")
        self.manager.request(dict(op="delivery", owner=OWNER, job_id="11111111", delivery="delivered"))
        result = self.manager.request(dict(op="delivery", owner=OWNER, job_id="11111111", delivery="failed", error="secret"))
        self.assertEqual(result["job"]["delivery"], "delivered")
        self.assertNotIn("secret", (self.root / "jobs" / "1111111111111111.json").read_text())

    async def test_queue_capacity_and_idempotency(self):
        self.program = fake_program("time.sleep(.15)", emit({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}))
        first = self.manager.request(request())
        self.assertEqual(self.manager.request(request()), first)
        collision = request()
        collision["prompt"] = "different"
        with self.assertRaisesRegex(ValueError, "collision"):
            self.manager.request(collision)
        collision["owner"] = OTHER
        with self.assertRaisesRegex(ValueError, "collision"):
            self.manager.request(collision)
        self.manager.request(request("2222222222222222"))
        self.manager.request(request("3333333333333333"))
        with self.assertRaisesRegex(ValueError, "queue is full"):
            self.manager.request(request("4444444444444444"))
        await asyncio.sleep(.04)
        self.assertEqual([j["state"] for j in self.manager.jobs.values()], ["running", "queued", "queued"])
        await self.finished("3333333333333333")
        self.assertTrue(all(j["state"] == "completed" for j in self.manager.jobs.values()))

    async def test_turn_failure_even_with_exit_zero_and_answer(self):
        self.program = fake_program(emit({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}), emit({"type": "turn.failed", "error": {"message": "s" + "k-secret123456789000"}}))
        self.manager.request(request())
        job = await self.finished()
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["result"])
        self.assertNotIn("secret", job["error"])

    async def test_transient_error_cleared_only_by_successful_turn(self):
        answer = emit({"type": "item.completed", "item": {"type": "agent_message", "text": "recovered"}})
        error = emit({"type": "error", "message": "transient transport failure"})
        completed = emit({"type": "turn.completed"})
        cases = (
            ([error, answer, completed], "completed"),
            ([error, answer], "failed"),
            ([emit({"type": "turn.failed"}), answer, completed], "failed"),
        )
        for index, (events, expected) in enumerate(cases):
            self.program = fake_program(*events)
            job_id = f"{index + 1:016x}"
            self.manager.request(request(job_id))
            self.assertEqual((await self.finished(job_id))["state"], expected)

    async def test_nonzero_exit_and_no_answer_fail(self):
        for index, program in enumerate((fake_program("sys.exit(2)"), fake_program(emit({"type": "turn.completed"})), fake_program("print('raw text, not a final event')"))):
            self.program = program
            job_id = f"{index + 1:016x}"
            self.manager.request(request(job_id))
            self.assertEqual((await self.finished(job_id))["state"], "failed")

    async def test_timeout(self):
        self.program = fake_program("time.sleep(5)")
        with patch.object(r, "TIMEOUT", .08):
            self.manager.request(request())
            job = await self.finished()
        self.assertEqual(job["state"], "timed_out")
        self.assertIsNotNone(job["finished_at"])

    async def test_final_answer_after_large_trace_and_oversized_line(self):
        self.program = fake_program(
            "for _ in range(10000): print(json.dumps({'type':'item.completed','item':{'type':'reasoning','text':'x'*100}}))",
            "print('x' * (2 * 1024 * 1024),flush=True)",
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": "late final " + "s" + "k-123456789000000000000"}}),
            emit({"type": "turn.completed"}),
        )
        self.manager.request(request())
        job = await self.finished()
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["result"], "late final [REDACTED]")

    async def test_heartbeat_and_progress_without_raw_commands(self):
        release = self.root / "release-heartbeat-test"
        self.program = fake_program(
            emit({"type": "item.started", "item": {"type": "command_execution", "command": "secret command"}}),
            "from pathlib import Path",
            f"release = Path({str(release)!r})",
            "while not release.exists(): time.sleep(.01)",
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        )
        self.manager.request(request())
        created = self.manager.jobs["1111111111111111"]["heartbeat_at"]
        deadline = time.monotonic() + 2
        status = self.manager.request(dict(op="status", owner=OWNER))["job"]
        while status["phase"] != "tool_running" and time.monotonic() < deadline:
            await asyncio.sleep(.01)
            status = self.manager.request(dict(op="status", owner=OWNER))["job"]
        self.assertEqual(status["phase"], "tool_running")
        heartbeat_before = status["heartbeat_at"]
        while status["heartbeat_at"] == heartbeat_before and time.monotonic() < deadline:
            await asyncio.sleep(.01)
            status = self.manager.request(dict(op="status", owner=OWNER))["job"]
        self.assertEqual(status["phase"], "tool_running")
        self.assertGreater(status["heartbeat_at"], heartbeat_before)
        self.assertGreater(status["heartbeat_at"], created)
        self.assertIsNotNone(status["last_event_at"])
        self.assertNotIn("secret command", json.dumps(status))
        release.touch()
        await self.finished()

    async def test_restart_preserves_completed_and_interrupts_active_without_rerun(self):
        self.manager.request(request())
        await self.finished()
        self.program = fake_program("time.sleep(5)")
        self.manager.request(request("2222222222222222"))
        self.manager.request(request("3333333333333333"))
        await asyncio.sleep(.05)
        # Snapshot as if the process stopped before it could write shutdown states.
        backup = {p.name: p.read_text() for p in (self.root / "jobs").glob("*.json")}
        await self.manager.close()
        for name, content in backup.items():
            (self.root / "jobs" / name).write_text(content)
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()
        self.assertEqual(self.manager.jobs["1111111111111111"]["state"], "completed")
        for job_id in ("2222222222222222", "3333333333333333"):
            self.assertEqual(self.manager.jobs[job_id]["state"], "interrupted")
        self.assertEqual(self.manager.queue.qsize(), 0)
        same = self.manager.request(request("2222222222222222"))
        self.assertEqual(same["job"]["state"], "interrupted")

    async def test_latest_and_prefix_ambiguity(self):
        self.manager.request(request("ffffffff00000000"))
        self.manager.request(request("ffffffff00000001"))
        self.assertEqual(self.manager.request(dict(op="status", owner=OWNER))["job"]["id"], "ffffffff00000001")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.manager.request(dict(op="status", owner=OWNER, job_id="ffffffff"))

    async def test_workspace_path_and_symlink_rejected(self):
        for workspace in ("../bad", "x" * 32, "/etc/passwd"):
            req = request()
            req["workspace"] = workspace
            with self.assertRaisesRegex(ValueError, "workspace"):
                self.manager.request(req)
        r.ROOT.mkdir(exist_ok=True)
        (r.ROOT / WORKSPACE).symlink_to(self.root)
        accepted = self.manager.request(request())["job"]
        self.assertEqual(accepted["execution_scope"], "isolated-conversation")

    async def test_shutdown_kills_descendant(self):
        pid_path = self.root / "descendant.pid"
        self.program = fake_program(
            "import subprocess",
            f"child=subprocess.Popen([sys.executable,'-c', \"import time,pathlib; time.sleep(.3); pathlib.Path({str(pid_path)!r}).write_text('leaked')\"])",
            "time.sleep(5)",
        )
        self.manager.request(request())
        await asyncio.sleep(.1)
        await self.manager.close()
        await asyncio.sleep(.35)
        self.assertFalse(pid_path.exists())
        self.assertEqual(self.manager.jobs["1111111111111111"]["state"], "interrupted")

    async def test_worker_failure_rejects_submit_and_fails_supervision(self):
        self.manager.worker.cancel()
        await asyncio.gather(self.manager.worker, return_exceptions=True)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.manager.request(request())
        with self.assertRaisesRegex(RuntimeError, "background task stopped"):
            await r.supervise(self.manager, asyncio.Event())
        self.assertTrue(self.manager.closing)

    async def test_heartbeat_failure_fails_supervision(self):
        self.manager.heartbeat_task.cancel()
        await asyncio.gather(self.manager.heartbeat_task, return_exceptions=True)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.manager.request(request())
        with self.assertRaisesRegex(RuntimeError, "background task stopped"):
            await r.supervise(self.manager, asyncio.Event())

    async def test_supervision_signal_stops_normally(self):
        stopped = asyncio.Event()
        stopped.set()
        await r.supervise(self.manager, stopped)
        self.assertFalse(self.manager.worker.done())
        self.assertFalse(self.manager.heartbeat_task.done())

    async def test_missing_wrapper_fails_without_unsandboxed_fallback(self):
        with patch.object(r, "command", return_value=[str(self.root / "missing-codex-bwrap")]):
            self.manager.request(request())
            job = await self.finished()
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["result"])
        self.assertIn("could not execute", job["error"])

    async def test_retention_bounds_terminal_records(self):
        self.manager.request(request())
        await self.finished()
        self.manager.request(request("2222222222222222"))
        await self.finished("2222222222222222")
        with patch.object(r, "MAX_TERMINAL", 1), patch.object(r.conversations, "MAX_CONVERSATIONS", 1):
            self.manager.prune()
        self.assertEqual(list(self.manager.jobs), ["2222222222222222"])
        self.assertEqual(len(list((self.root / "jobs").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()

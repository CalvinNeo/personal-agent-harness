"""Durable Codex sessions: real process events with synthetic images only."""
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from PIL import Image

spec = importlib.util.spec_from_file_location("conversation_runner", Path(__file__).with_name("codex-runner.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
OWNER, OTHER, WORKSPACE = "a" * 64, "b" * 64, "c" * 32
ROOT_JOB, CHILD_JOB, THIRD_JOB = "1" * 16, "2" * 16, "3" * 16
THREAD = "11111111-1111-4111-8111-111111111111"


def image(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(stream, format="PNG")
    return {"mime": "image/png", "data": base64.b64encode(stream.getvalue()).decode()}


def request(jid=ROOT_JOB, **kwargs):
    return {"op": "submit", "job_id": jid, "owner": OWNER, "workspace": WORKSPACE,
            "mode": "read-only", "prompt": "Synthetic task", **kwargs}


class Sessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root_patch = patch.object(r, "ROOT", self.root / "workspaces")
        self.root_patch.start()
        self.calls, self.delay, self.fail_resume, self.wrong_thread = [], 0, False, False

        def fake_command(workspace, mode, image_paths=None, **kwargs):
            home = kwargs.get("session_home")
            resume = kwargs.get("resume_thread_id")
            self.calls.append({"workspace": workspace, "mode": mode, "images": list(image_paths or []), **kwargs})
            tid = "22222222-2222-4222-8222-222222222222" if self.wrong_thread and resume else THREAD
            script = "import sys,json,time; from pathlib import Path; sys.stdin.read(); "
            if self.fail_resume and resume:
                script += "sys.exit(2)"
            else:
                # Simulate Codex creating private state and emitting its actual
                # session id. The production runner parses these real events.
                script += f"Path({str(home / 'state.synthetic')!r}).write_text('persistent-session'); "
                script += f"print(json.dumps({{'type': 'thread.started', 'thread_id': {tid!r}}}),flush=True); time.sleep({self.delay}); "
                script += "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'synthetic result'}}),flush=True); "
                script += "print(json.dumps({'type':'turn.completed'}),flush=True)"
            return [sys.executable, "-c", script]

        self.command_patch = patch.object(r, "command", fake_command)
        self.command_patch.start()
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()

    async def asyncTearDown(self):
        await self.manager.close()
        self.command_patch.stop()
        self.root_patch.stop()
        self.temp.cleanup()

    async def done(self, jid):
        async with asyncio.timeout(4):
            while self.manager.jobs[jid]["state"] in r.ACTIVE:
                await asyncio.sleep(.01)
        return self.manager.jobs[jid]

    async def test_resume_reuses_real_thread_and_images_after_restart(self):
        first = self.manager.request(request(images=[image()]))["job"]
        self.assertTrue(first["resumable"])
        await self.done(ROOT_JOB)
        initial = self.calls[0]
        self.assertTrue(initial["images"][0].exists())
        self.assertNotIn("thread_id", self.manager.public(self.manager.jobs[ROOT_JOB]))
        await self.manager.close()
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()
        child = self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))["job"]
        self.assertEqual(child["conversation_id"], ROOT_JOB)
        self.assertEqual(child["parent_job_id"], ROOT_JOB)
        self.assertEqual(child["input_images"], 0)
        self.assertEqual(child["context_images"], 1)
        self.assertEqual((await self.done(CHILD_JOB))["state"], "completed")
        self.assertEqual(self.calls[1]["resume_thread_id"], THREAD)
        self.assertEqual(self.calls[1]["session_home"], initial["session_home"])
        self.assertEqual(self.calls[1]["images"], initial["images"])

    async def test_queued_parent_resolves_thread_at_execution(self):
        self.delay = .1
        self.manager.request(request())
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        self.assertEqual((await self.done(CHILD_JOB))["state"], "completed")
        self.assertIsNone(self.calls[0]["resume_thread_id"])
        self.assertEqual(self.calls[1]["resume_thread_id"], THREAD)

    async def test_owner_isolation_legacy_and_no_mode_escalation(self):
        self.manager.request(request())
        await self.done(ROOT_JOB)
        with self.assertRaisesRegex(ValueError, "not found"):
            self.manager.request(request(CHILD_JOB, owner=OTHER, parent_job_id=ROOT_JOB))
        with self.assertRaisesRegex(ValueError, "cannot elevate"):
            self.manager.request(request(CHILD_JOB, mode="workspace-write", parent_job_id=ROOT_JOB))
        legacy = {**self.manager.jobs[ROOT_JOB], "id": "f" * 16}
        legacy.pop("conversation_id")
        self.manager.jobs[legacy["id"]] = legacy
        with self.assertRaisesRegex(ValueError, "legacy_unavailable"):
            self.manager.request(request(CHILD_JOB, parent_job_id=legacy["id"]))
        self.manager.request(request(CHILD_JOB, owner=OTHER))
        await self.done(CHILD_JOB)
        self.assertNotEqual(self.calls[0]["session_home"], self.calls[1]["session_home"])
        self.assertNotEqual(self.calls[0]["workspace"], self.calls[1]["workspace"])

    async def test_resume_failure_does_not_start_a_new_session(self):
        self.manager.request(request())
        await self.done(ROOT_JOB)
        self.fail_resume = True
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        self.assertEqual((await self.done(CHILD_JOB))["state"], "failed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["resume_thread_id"], THREAD)
        self.assertEqual(self.manager.conversations.items[ROOT_JOB]["thread_id"], THREAD)

    async def test_changed_thread_is_rejected(self):
        self.manager.request(request())
        await self.done(ROOT_JOB)
        self.wrong_thread = True
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        self.assertEqual((await self.done(CHILD_JOB))["state"], "failed")
        self.assertEqual(self.manager.conversations.items[ROOT_JOB]["thread_id"], THREAD)

    async def test_cancel_and_restart_keep_started_session_resumable(self):
        self.delay = 10
        self.manager.request(request(images=[image()]))
        async with asyncio.timeout(2):
            while not self.manager.conversations.items[ROOT_JOB].get("thread_id"):
                await asyncio.sleep(.01)
        await self.manager.close()
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()
        self.assertEqual(self.manager.jobs[ROOT_JOB]["state"], "interrupted")
        self.assertTrue(self.manager.public(self.manager.jobs[ROOT_JOB])["resumable"])
        self.delay = 0
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        self.assertEqual((await self.done(CHILD_JOB))["state"], "completed")

    async def test_latest_activity_retention_and_image_bound(self):
        self.manager.request(request(images=[image(c) for c in ("red", "blue", "green", "yellow")]))
        await self.done(ROOT_JOB)
        with self.assertRaisesRegex(ValueError, "image_limit"):
            self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB, images=[image("black")]))
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB, images=[image("red")]))
        await self.done(CHILD_JOB)
        item = self.manager.conversations.items[ROOT_JOB]
        item["created"] = time.time() - 20 * 86400
        self.manager.prune()
        self.assertTrue(self.manager.public(self.manager.jobs[ROOT_JOB])["resumable"])
        item["updated"] = time.time() - 8 * 86400
        self.manager.prune()
        self.assertFalse(self.manager.public(self.manager.jobs[ROOT_JOB])["resumable"])
        self.assertFalse(self.manager.conversations.root(item).exists())
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.manager.request(request(THIRD_JOB, parent_job_id=CHILD_JOB))

    async def test_storage_bound_retires_session_and_originals(self):
        self.manager.request(request(images=[image()]))
        await self.done(ROOT_JOB)
        item = self.manager.conversations.items[ROOT_JOB]
        folder = self.manager.conversations.root(item)
        (folder / "workspace" / "oversized.bin").write_bytes(b"x" * 2048)
        with patch.object(r.conversations, "MAX_BYTES", 1024):
            self.manager.prune()
        self.assertFalse(folder.exists())
        self.assertFalse(self.manager.public(self.manager.jobs[ROOT_JOB])["resumable"])

    async def test_prior_outputs_preserved_but_not_redelivered(self):
        self.manager.request(request(images=[image()]))
        await self.done(ROOT_JOB)
        item = self.manager.conversations.items[ROOT_JOB]
        workspace = self.manager.conversations.root(item) / "workspace"
        (workspace / "output" / "annotation.png").write_bytes(base64.b64decode(image()["data"]))
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        child = await self.done(CHILD_JOB)
        self.assertTrue((workspace / "previous-output" / ROOT_JOB / "annotation.png").is_file())
        self.assertEqual(child["artifacts"], [])

    async def test_readonly_followup_does_not_inherit_write_access(self):
        self.manager.request(request(mode="workspace-write"))
        await self.done(ROOT_JOB)
        self.manager.request(request(CHILD_JOB, parent_job_id=ROOT_JOB))
        await self.done(CHILD_JOB)
        self.assertEqual([call["mode"] for call in self.calls], ["workspace-write", "read-only"])


class CommandTests(unittest.TestCase):
    def test_persistent_home_and_actual_resume_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home, workspace = root / "home", root / "workspace"
            home.mkdir(); workspace.mkdir()
            cmd = r.command(workspace, "read-only", session_home=home, resume_thread_id=THREAD)
            self.assertNotIn("--ephemeral", cmd)
            self.assertIn("resume", cmd)
            self.assertEqual(cmd[-3:], ["--", THREAD, "-"])
            i = cmd.index(str(home))
            self.assertEqual(cmd[i-1:i+2], ["--bind", str(home), r.CODEX_HOME])
            self.assertIn("--ro-bind", cmd)
            self.assertIn("--unshare-pid", cmd)


if __name__ == "__main__":
    unittest.main()

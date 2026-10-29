"""Synthetic images and mocked Codex only; no model, network, or QQ calls."""
import asyncio
import base64
import importlib.util
import io
import json
import os
import random
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, PngImagePlugin

spec = importlib.util.spec_from_file_location("image_runner", Path(__file__).with_name("codex-runner.py"))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
m = r.media
OWNER, OTHER, WORKSPACE, JID = "a" * 64, "b" * 64, "c" * 32, "1" * 16


def png(color="red", size=(32, 32)):
    stream = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("private", "metadata-should-not-survive")
    Image.new("RGB", size, color).save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


def image_input(color="red"):
    return {"mime": "image/png", "data": base64.b64encode(png(color)).decode()}


def request(jid=JID, **kwargs):
    return {"op": "submit", "owner": OWNER, "workspace": WORKSPACE,
            "job_id": jid, "mode": "read-only", "prompt": "Label the original image",
            "images": [image_input()], **kwargs}


class MediaTests(unittest.TestCase):
    def test_low_quality_jpeg_is_not_reencoded_above_limit(self):
        noise = Image.frombytes("RGB", (1024, 1024), random.Random(1).randbytes(1024 * 1024 * 3))
        stream = io.BytesIO()
        noise.save(stream, format="JPEG", quality=40, optimize=True)
        raw = stream.getvalue()
        self.assertLess(len(raw), m.MAX_INPUT_BYTES)
        source = Image.open(io.BytesIO(raw))
        inflated = io.BytesIO()
        source.save(inflated, format="JPEG", quality=90)
        self.assertGreater(len(inflated.getvalue()), m.MAX_INPUT_BYTES)
        normalized = m.normalize(raw, "image/jpeg", m.MAX_INPUT_BYTES)
        self.assertLessEqual(len(normalized), m.MAX_INPUT_BYTES)
        self.assertEqual(Image.open(io.BytesIO(normalized)).size, (1024, 1024))

    def test_inputs_are_bytes_only_and_normalized(self):
        images = m.decode_inputs([image_input()])
        self.assertEqual(len(images), 1)
        self.assertNotIn(b"metadata-should-not-survive", images[0]["bytes"])
        for bad in ({"path": "/etc/passwd"}, {"url": "https://example.com/image.png"},
                    {**image_input(), "path": "/etc/passwd"},
                    {"mime": "image/jpeg", "data": image_input()["data"]},
                    {"mime": "image/png", "data": "!!!!"},
                    {"mime": "image/png", "data": base64.b64encode(b"not an image").decode()}):
            with self.subTest(bad=bad.keys()), self.assertRaises(ValueError):
                m.decode_inputs([bad])
        with self.assertRaises(ValueError):
            m.decode_inputs([image_input()] * 5)
        with self.assertRaises(ValueError):
            m.decode_inputs([{"mime": "image/png", "data": "A" * (1024 * 1024)}])
        with patch.object(m, "MAX_PIXELS", 16):
            with self.assertRaises(ValueError):
                m.decode_inputs([image_input()])

    def test_outputs_reject_links_and_nonimages_and_strip_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workspace = root / "workspace"
            workspace.mkdir(exist_ok=True)
            inputs = m.stage_inputs(workspace, m.decode_inputs([image_input()]))
            output = workspace / "output"
            (output / "a-link.png").symlink_to(inputs[0])
            os.link(inputs[0], output / "b-hardlink.png")
            (output / "c-fake.png").write_bytes(b"secret text")
            (output / "d-valid.png").write_bytes(png())
            (output / "e-directory.png").mkdir()
            artifacts = m.collect_artifacts(workspace, root / "archive")
            self.assertEqual(artifacts, [{"id": "0", "mime": "image/png", "name": "result-1.png"}])
            payload = m.artifact_payload(root / "archive", artifacts[0])
            raw = base64.b64decode(payload["data"])
            self.assertNotIn(b"metadata-should-not-survive", raw)
            self.assertEqual((root / "archive" / "0.png").stat().st_mode & 0o777, 0o600)

    def test_output_count_is_bounded_and_input_mount_is_readonly(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            m.stage_inputs(root, m.decode_inputs([image_input()]))
            for i in range(6):
                (root / "output" / f"{i}.png").write_bytes(png())
            artifacts = m.collect_artifacts(root, root / "archive")
            self.assertEqual(len(artifacts), 3)
            command = r.command(root, "workspace-write", [root / "input" / "0.png"])
            index = command.index(str(root / "input"))
            self.assertEqual(command[index - 1], "--ro-bind")
            self.assertIn("--image", command)
            self.assertIn('web_search="live"', command)
            self.assertEqual(command[-2:], ["--", "-"])
            with self.assertRaises(ValueError):
                r.command(root, "workspace-write", [Path("/etc/passwd")])


class ImageJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch_root = patch.object(r, "ROOT", self.root / "workspaces")
        self.patch_root.start()
        self.seen = []
        self.release = asyncio.Event()

        async def fake(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            kwargs["on_thread"]("11111111-1111-4111-8111-111111111111")
            self.seen.append((mode, workspace, image_paths))
            self.assertEqual(mode, "workspace-write")
            self.assertTrue(all(path.is_file() for path in image_paths))
            self.assertEqual([path.parent.name for path in image_paths], ["input"])
            await self.release.wait()
            (workspace / "output" / "annotated.png").write_bytes(png("blue"))
            return "Annotation completed"

        self.fake = patch.object(r, "run_codex", fake)
        self.fake.start()
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()

    async def asyncTearDown(self):
        self.release.set()
        await self.manager.close()
        self.fake.stop()
        self.patch_root.stop()
        self.temp.cleanup()

    async def finish(self, jid=JID):
        self.release.set()
        async with asyncio.timeout(3):
            while self.manager.jobs[jid]["state"] in r.ACTIVE:
                await asyncio.sleep(.01)
        return self.manager.jobs[jid]

    async def test_image_job_scope_idempotency_owner_and_artifact_roundtrip(self):
        req = request()
        first = self.manager.request(req)["job"]
        self.assertEqual(first["mode"], "read-only")
        self.assertEqual(first["execution_scope"], "isolated-image-task")
        self.assertEqual(first["input_images"], 1)
        self.assertEqual(self.manager.request(req)["job"]["id"], JID)
        for change in ({"images": [image_input("blue")]}, {"owner": OTHER}):
            with self.assertRaisesRegex(ValueError, "collision"):
                self.manager.request({**req, **change})
        saved = (self.root / "jobs" / (JID + ".json")).read_text()
        self.assertNotIn(req["images"][0]["data"], saved)
        self.assertNotIn('"_images"', saved)
        self.assertFalse((r.ROOT / WORKSPACE).exists())
        final = await self.finish()
        self.assertEqual(final["state"], "completed")
        self.assertEqual(len(final["artifacts"]), 1)
        workspace = self.seen[0][1]
        self.assertEqual(workspace, self.manager.conversations.root(self.manager.conversations.items[JID]) / "workspace")
        self.assertTrue(workspace.exists())
        read = {"op": "artifact", "owner": OWNER, "job_id": JID, "artifact_id": "0"}
        payload = self.manager.request(read)["artifact"]
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(payload["data"]))).getpixel((0, 0)), (0, 0, 255))
        with self.assertRaisesRegex(ValueError, "not found"):
            self.manager.request({**read, "owner": OTHER})
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.manager.request({**read, "artifact_id": "../../etc/passwd"})
        with patch.object(r, "RETENTION", -1), patch.object(r.conversations, "RETENTION", -1):
            self.manager.prune()
        self.assertFalse((self.root / "jobs" / "artifacts" / JID).exists())

    async def test_image_task_workspace_never_reused(self):
        candidate = {"id": JID, "owner": OWNER}
        path = self.manager.conversations.root(candidate)
        path.mkdir()
        (path / "old-data").write_text("old")
        with self.assertRaises(FileExistsError):
            self.manager.request(request())
        self.assertFalse(self.manager.jobs)

    async def test_restart_cleans_interrupted_inputs_and_does_not_rerun(self):
        self.manager.request(request())
        async with asyncio.timeout(2):
            while not self.seen:
                await asyncio.sleep(.01)
        workspace = self.seen[0][1]
        snapshot = (self.root / "jobs" / (JID + ".json")).read_text()
        await self.manager.close()
        workspace.mkdir(exist_ok=True)
        (workspace / "leftover.png").write_bytes(png())
        (self.root / "jobs" / (JID + ".json")).write_text(snapshot)
        self.manager = r.JobManager(self.root / "jobs")
        await self.manager.start()
        self.assertEqual(self.manager.jobs[JID]["state"], "interrupted")
        self.assertTrue(workspace.exists())
        self.assertEqual(self.manager.queue.qsize(), 0)

    async def test_failure_cleans_private_inputs_and_does_not_return_artifacts(self):
        async def fail(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            (workspace / "output" / "partial.png").write_bytes(png())
            hidden = workspace / "private"
            hidden.mkdir()
            (hidden / "data").write_text("temporary")
            hidden.chmod(0)
            raise r.JobFailure("failed", "synthetic failure")
        with patch.object(r, "run_codex", fail):
            self.manager.request(request())
            final = await self.finish()
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["artifacts"], [])
        self.assertFalse(r.image_workspace(OWNER, JID).exists())
        self.assertNotIn("_images", final)

    async def test_missing_outputs_cannot_claim_an_image_was_returned(self):
        async def no_output(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            return "已生成标注图，保存到 result.png"
        with patch.object(r, "run_codex", no_output):
            self.manager.request(request())
            final = await self.finish()
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["artifact_status"], "none")
        self.assertTrue(final["result"].startswith("【未生成结果图片】"))
        self.assertEqual(final["artifacts"], [])

    async def test_invalid_output_fails_instead_of_silent_success(self):
        async def invalid_output(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            (workspace / "output" / "a-valid.png").write_bytes(png())
            (workspace / "output" / "b-invalid.png").write_bytes(b"not a real image")
            return "已生成标注图"
        with patch.object(r, "run_codex", invalid_output):
            self.manager.request(request())
            final = await self.finish()
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["artifact_status"], "rejected")
        self.assertIsNone(final["result"])
        self.assertEqual(final["artifacts"], [])
        self.assertIn("validation", final["error"])
        self.assertFalse((self.root / "jobs" / "artifacts" / JID).exists())

    async def test_distinct_owners_and_jobs_get_distinct_workspaces(self):
        self.assertNotEqual(r.image_workspace(OWNER, JID), r.image_workspace(OTHER, JID))
        self.assertNotEqual(r.image_workspace(OWNER, JID), r.image_workspace(OWNER, "2" * 16))

    async def test_large_valid_rpc_request_can_exceed_old_64k_limit(self):
        noise = Image.frombytes("RGB", (180, 180), os.urandom(180 * 180 * 3))
        stream = io.BytesIO()
        noise.save(stream, format="PNG")
        req = request(images=[{"mime": "image/png", "data": base64.b64encode(stream.getvalue()).decode()}])
        data = json.dumps(req).encode() + b"\n"
        self.assertGreater(len(data), 65536)
        sock = str(self.root / "bridge.sock")
        with patch.object(r, "peer_uid", return_value=996):
            server = await asyncio.start_unix_server(lambda reader, writer: r.handle(reader, writer, self.manager), path=sock, limit=r.MAX_REQUEST)
            async with server:
                reader, writer = await asyncio.open_unix_connection(sock)
                writer.write(data)
                await writer.drain()
                self.assertTrue(json.loads(await reader.readline())["ok"])
                writer.close()
                await writer.wait_closed()
        await self.finish()


if __name__ == "__main__":
    unittest.main()

import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_media import png, r as runner, request, JID, OWNER
import test_media as fixture


media = runner.media
SAMPLE = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"synthetic video body"
PROBE = {"format": {"duration": "1.0"}, "streams": [
    {"codec_type": "video", "codec_name": "h264", "width": 160, "height": 90},
    {"codec_type": "audio", "codec_name": "aac"},
]}


class OutputMediaTests(unittest.TestCase):
    def test_video_validation_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(media.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(PROBE).encode())) as probe:
            root = Path(directory)
            (root / "output").mkdir()
            (root / "output" / "demo.mp4").write_bytes(SAMPLE)
            artifacts = media.collect_artifacts(root, root / "archive", strict=True)
            self.assertEqual(artifacts[0]["mime"], "video/mp4")
            payload = media.artifact_payload(root / "archive", artifacts[0])
            self.assertEqual(base64.b64decode(payload["data"]), SAMPLE)
            self.assertEqual(payload["sha256"], hashlib.sha256(SAMPLE).hexdigest())
            command = probe.call_args.args[0]
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "pipe")
            self.assertEqual(command[command.index("-enable_drefs") + 1], "0")
            (root / "archive" / "0.mp4").write_bytes(SAMPLE + b"changed")
            with self.assertRaisesRegex(ValueError, "integrity"):
                media.artifact_payload(root / "archive", artifacts[0])

    def test_invalid_video_content_codec_duration_and_decoder_fail_closed(self):
        for data in (b"not video", b"\x00" * (media.MAX_VIDEO_BYTES + 1)):
            with self.assertRaises(ValueError):
                media.validate_video(data)
        invalid = [
            {"format": {"duration": "NaN"}, "streams": PROBE["streams"]},
            {"format": {"duration": "601"}, "streams": PROBE["streams"]},
            {"format": {"duration": "1"}, "streams": []},
            {"format": {"duration": "1"}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 100, "height": 100}]},
            {"format": {"duration": "1"}, "streams": [*PROBE["streams"], {"codec_type": "data"}]},
        ]
        for metadata in invalid:
            with self.subTest(metadata=metadata), patch.object(media.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(metadata).encode())):
                with self.assertRaises(ValueError):
                    media.validate_video(SAMPLE)
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("ffprobe", 15), subprocess.CalledProcessError(1, "ffprobe")):
            with patch.object(media.subprocess, "run", side_effect=error), self.assertRaises(ValueError):
                media.validate_video(SAMPLE)

    def test_excess_and_unsupported_media_are_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            for number in range(4):
                (root / "output" / (str(number) + ".png")).write_bytes(png())
            with self.assertRaisesRegex(media.ArtifactError, "At most 3"):
                media.collect_artifacts(root, root / "archive", strict=True)
            for path in (root / "output").iterdir():
                path.unlink()
            (root / "output" / "animation.webm").write_bytes(b"unsupported")
            with self.assertRaisesRegex(media.ArtifactError, "Unsupported"):
                media.collect_artifacts(root, root / "archive", strict=True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg runtime is required")
    def test_real_mp4_can_be_generated_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "output").mkdir()
            path = root / "output" / "clip.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=10", "-t", "1",
                            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)],
                           check=True, timeout=30, capture_output=True)
            artifacts = media.collect_artifacts(root, root / "archive", strict=True)
            payload = media.artifact_payload(root / "archive", artifacts[0])
            self.assertEqual(payload["mime"], "video/mp4")


class NestedSandboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_protected_subdirectories_exist_before_codex_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checked = []

            def command(workspace, mode, image_paths=None, **kwargs):
                for folder in (workspace, workspace / ".tmp", workspace / "output"):
                    for name in (".git", ".agents", ".codex", ".aws"):
                        self.assertTrue((folder / name).is_dir())
                checked.append(True)
                import sys
                return [sys.executable, "-c", 'import sys,json;sys.stdin.read();print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}));print(json.dumps({"type":"turn.completed"}))']

            with patch.object(runner, "command", command):
                result = await runner.run_codex("synthetic", "read-only", root, lambda phase: None)
            self.assertEqual(result, "done")
            self.assertEqual(checked, [True])


class TextOutputJobTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.ImageJobTests.asyncSetUp
    asyncTearDown = fixture.ImageJobTests.asyncTearDown
    finish = fixture.ImageJobTests.finish

    async def test_text_only_readonly_job_returns_generated_image(self):
        async def generate(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            self.assertEqual(mode, "read-only")
            self.assertIsNone(image_paths)
            (workspace / "output" / "diagram.png").write_bytes(png())
            return "Created a diagram"
        with patch.object(runner, "run_codex", generate):
            self.manager.request(request(images=[]))
            final = await self.finish()
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["artifact_status"], "ready")
        self.assertEqual(final["artifacts"][0]["mime"], "image/png")
        payload = self.manager.request({"op": "artifact", "owner": OWNER, "job_id": JID, "artifact_id": "0"})
        self.assertTrue(payload["ok"])

    async def test_text_only_job_returns_video(self):
        async def generate(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            (workspace / "output" / "clip.mp4").write_bytes(SAMPLE)
            return "Created a video"
        with patch.object(runner, "run_codex", generate), patch.object(media, "validate_video", return_value=SAMPLE):
            self.manager.request(request(images=[]))
            final = await self.finish()
        self.assertEqual(final["state"], "completed")
        self.assertEqual(final["artifacts"][0]["mime"], "video/mp4")

    async def test_live_memory_tools_do_not_depend_on_eager_snapshot_file(self):
        async def inspect(prompt, mode, workspace, progress, image_paths=None, **kwargs):
            self.assertIsNone(kwargs["memory_path"])
            self.assertTrue(kwargs["capability_path"].is_dir())
            return "used live tools"
        tools = [{"name": "list_my_long_term_memories", "description": "live", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}]
        with patch.object(runner, "run_codex", inspect):
            self.manager.request(request(images=[], capability_tools=tools))
            final = await self.finish()
        self.assertEqual(final["state"], "completed")


if __name__ == "__main__":
    unittest.main()

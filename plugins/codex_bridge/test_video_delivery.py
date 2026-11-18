import base64
import hashlib
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import test_plugin as fixture


media = importlib.import_module(fixture.plugin.__package__ + ".media")
SAMPLE = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"synthetic body"


def video_payload(data=SAMPLE):
    return {"mime": "video/mp4", "name": "../../untrusted-name.mp4", "data": base64.b64encode(data).decode(),
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class Video:
    @staticmethod
    def fromFileSystem(path):
        instance = Video()
        instance.path = path
        instance.file = Path(path).as_uri()
        return instance


class VideoDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.Tests.setUp(self)
        self.component_patch = patch.object(sys.modules["astrbot.api.message_components"], "Video", Video, create=True)
        self.component_patch.start()
        self.p._rpc = AsyncMock(return_value={"ok": True, "artifact": video_payload()})

    def tearDown(self):
        self.component_patch.stop()
        fixture.Tests.tearDown(self)

    async def test_native_video_is_scoped_forwarded_and_temporary_file_removed(self):
        route = fixture.Tests.route(self)
        source = fixture.Event()
        self.p._events[fixture.JID] = source
        paths = []

        async def send(chain):
            for part in chain.chain:
                if isinstance(part, Video):
                    path = Path(part.path)
                    self.assertEqual(path.read_bytes(), SAMPLE)
                    self.assertEqual(path.parent, self.p.routes_file.parent)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    paths.append(path)
            return True

        source.send = AsyncMock(side_effect=send)
        job = fixture.job("completed", artifacts=[{"id": "0", "mime": "video/mp4"}])
        self.assertTrue(await self.p._deliver_final(route, job))
        self.assertEqual(route["artifacts_delivered"], ["0"])
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertEqual(self.p._artifact_files, {})
        self.p._rpc.assert_awaited_once_with({"op": "artifact", "owner": route["owner"], "job_id": fixture.JID, "artifact_id": "0"})
        self.assertTrue(await self.p._deliver_final(route, job))
        self.assertEqual(source.send.await_count, 2)

    async def test_failed_video_send_is_retried_without_resending_text(self):
        route = fixture.Tests.route(self)
        source = fixture.Event()
        self.p._events[fixture.JID] = source
        source.send = AsyncMock(side_effect=[True, False])
        self.context.send_message = AsyncMock(return_value=False)
        job = fixture.job("completed", artifacts=[{"id": "0", "mime": "video/mp4"}])
        self.assertFalse(await self.p._deliver_final(route, job))
        self.assertTrue(route["text_delivered"])
        self.assertEqual(route.get("artifacts_delivered", []), [])
        self.assertEqual(list(self.p.routes_file.parent.glob(".codex-media-*.mp4")), [])
        self.context.send_message.return_value = True
        self.assertTrue(await self.p._deliver_final(route, job))
        self.assertEqual(route["artifacts_delivered"], ["0"])
        self.assertEqual(source.send.await_count, 2)
        self.assertEqual(self.context.send_message.await_count, 2)
        self.assertEqual(list(self.p.routes_file.parent.glob(".codex-media-*.mp4")), [])

    async def test_mime_mismatch_or_tampered_video_never_reaches_qq(self):
        for artifact in ({**video_payload(), "mime": "image/png"}, {**video_payload(), "sha256": "wrong"}):
            self.p._rpc.return_value = {"ok": True, "artifact": artifact}
            with self.assertRaises((RuntimeError, media.ContextError)):
                await self.p._artifact_chain(fixture.job("completed"), "a" * 64, {"id": "0", "mime": "video/mp4"})
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.p._artifact_files, {})

    async def test_manual_retrieval_cleans_video_file_after_send_failure(self):
        artifact_job = fixture.job("completed", artifacts=[{"id": "0", "mime": "video/mp4"}])
        self.p._rpc.side_effect = [{"ok": True, "job": artifact_job}, {"ok": True, "artifact": video_payload()}]
        source = fixture.Event()
        source.send = AsyncMock(side_effect=[True, False, True])
        await self.p._show(source, "result", fixture.JID)
        self.assertEqual(self.p._artifact_files, {})
        self.assertEqual(list(self.p.routes_file.parent.glob(".codex-media-*.mp4")), [])


class VideoDecoderTests(unittest.TestCase):
    def test_validates_bytes_length_digest_mime_and_base64(self):
        self.assertEqual(media.decode_video_artifact(video_payload()), SAMPLE)
        invalid = [None, {}, {**video_payload(), "size": 0}, {**video_payload(), "sha256": "bad"},
                   {**video_payload(), "data": "invalid!"}, video_payload(b"not a video"),
                   {**video_payload(), "data": "a" * (4 * ((media.MAX_VIDEO_BYTES + 2) // 3) + 1)}]
        for payload in invalid:
            with self.subTest(payload_type=type(payload)), self.assertRaises(media.ContextError):
                media.decode_video_artifact(payload)


if __name__ == "__main__":
    unittest.main()

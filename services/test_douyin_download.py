"""No network: task-local media handles, bounded downloads and safe file cleanup."""
import asyncio
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from multidict import CIMultiDict

import douyin_download as download
from public_web import WebError


MEDIA = "https://v26-web.douyinvod.com/video/example/?signature=public-media-signature"


class Response:
    def __init__(self, body=b"synthetic-video", code=200, headers=None):
        self.body, self.status = body, code
        self.headers = CIMultiDict({"Content-Type": "video/mp4", **(headers or {})})
        self.content_length = int(self.headers["Content-Length"]) if "Content-Length" in self.headers else None
        self.content = self

    async def iter_chunked(self, size):
        yield self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        self.scratch = self.workspace / ".tmp"
        self.scratch.mkdir(mode=0o700)
        self.patches = [patch.object(download, "_resources", {}), patch.object(download, "_downloaded", 0),
                        patch.object(download, "_busy", False), patch.object(download.Path, "cwd", return_value=self.workspace),
                        patch.dict(os.environ, {"TMPDIR": str(self.scratch)})]
        for value in self.patches:
            value.start()

    async def asyncTearDown(self):
        for value in reversed(self.patches):
            value.stop()
        self.temporary.cleanup()

    def register(self, size=100):
        result = download.register_media({"ok": True, "notice": "test", "media": [
            {"kind": "video", "urls": [MEDIA], "size_bytes": size}]})
        return result["media"][0]["resource_id"]

    async def test_only_advertised_resource_handles_are_accepted(self):
        with patch.object(download, "save_resource", new_callable=AsyncMock) as save:
            for invalid in (None, "unknown", MEDIA, "../input/file", ""):
                self.assertEqual(await download.download_douyin_media(invalid), {"ok": False, "error": "douyin_unknown_media_resource"})
            save.assert_not_called()
        self.assertFalse(list(self.scratch.iterdir()))

    async def test_success_writes_private_task_local_file_without_echoing_signed_url(self):
        handle = self.register()

        async def save(resource, destination):
            self.assertEqual(resource["urls"], (MEDIA,))
            destination.write(b"video")
            return {"bytes": 5, "sha256": hashlib.sha256(b"video").hexdigest(), "content_type": "video/mp4"}

        with patch.object(download, "save_resource", side_effect=save):
            result = await download.download_douyin_media(handle)
        self.assertTrue(result["ok"])
        output = Path(result["path"])
        self.assertEqual(output.parent, self.scratch)
        self.assertEqual(output.read_bytes(), b"video")
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["relative_path"], ".tmp/" + output.name)
        self.assertNotIn("signature", str(result))
        self.assertIn("Downloaded only", result["notice"])

    async def test_failure_and_cancellation_remove_only_created_partial(self):
        handle = self.register()
        for error in (WebError("douyin_media_empty"), TimeoutError(), asyncio.CancelledError()):
            async def save(resource, destination):
                destination.write(b"partial")
                raise error
            with patch.object(download, "save_resource", side_effect=save):
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await download.download_douyin_media(handle)
                else:
                    self.assertFalse((await download.download_douyin_media(handle))["ok"])
            self.assertFalse(list(self.scratch.iterdir()))
            self.assertFalse(download._busy)

    async def test_existing_file_or_symlink_is_not_overwritten_or_removed(self):
        handle = self.register()
        filename = self.scratch / "douyin-collision.mp4"
        filename.write_text("user data")
        with patch.object(download.secrets, "token_hex", return_value="collision"), \
             patch.object(download, "save_resource", new_callable=AsyncMock) as save:
            self.assertFalse((await download.download_douyin_media(handle))["ok"])
            save.assert_not_called()
        self.assertEqual(filename.read_text(), "user data")
        filename.unlink()
        target = self.workspace / "user.txt"
        target.write_text("original")
        filename.symlink_to(target)
        with patch.object(download.secrets, "token_hex", return_value="collision"):
            self.assertFalse((await download.download_douyin_media(handle))["ok"])
        self.assertTrue(filename.is_symlink())
        self.assertEqual(target.read_text(), "original")

    async def test_missing_external_and_symlink_scratch_are_rejected(self):
        handle = self.register()
        for directory in ("", "/tmp", str(self.workspace / "other/.tmp")):
            with patch.dict(os.environ, {"TMPDIR": directory}):
                self.assertEqual((await download.download_douyin_media(handle))["error"], "douyin_task_scratch_unavailable")
        self.scratch.rmdir()
        self.scratch.symlink_to(self.workspace, target_is_directory=True)
        self.assertEqual((await download.download_douyin_media(handle))["error"], "douyin_task_scratch_unavailable")

    async def test_size_limit_busy_and_registry_limit(self):
        result = download.register_media({"ok": True, "notice": "test", "media": [
            {"kind": "video", "urls": [MEDIA], "size_bytes": download.MAX_DOWNLOAD + 1}]})
        self.assertFalse(result["media"][0]["download_available"])
        self.assertNotIn("resource_id", result["media"][0])
        handle = self.register()
        download._resources[handle]["size_bytes"] = download.MAX_DOWNLOAD + 1
        self.assertEqual((await download.download_douyin_media(handle))["error"], "douyin_media_too_large")
        with patch.object(download, "_busy", True):
            self.assertEqual((await download.download_douyin_media(handle))["error"], "douyin_media_download_busy")
        with patch.object(download, "MAX_HANDLES", 1):
            result = download.register_media({"ok": True, "notice": "test", "media": [{"kind": "video", "urls": [MEDIA]}]})
        self.assertFalse(result["media"][0]["download_available"])
        self.assertEqual(len(download._resources), 1)

    async def test_http_download_is_get_only_and_has_no_cookies_or_account_credentials(self):
        response = Response()
        client = Mock()
        client.get.return_value = response
        session = AsyncMock()
        session.__aenter__.return_value = client
        output = io.BytesIO()
        with patch.object(download.aiohttp, "ClientSession", return_value=session) as factory, \
             patch.object(download.aiohttp, "TCPConnector"):
            result = await download.save_resource({"urls": (MEDIA,)}, output)
        self.assertEqual(result["bytes"], len(response.body))
        self.assertEqual(output.getvalue(), response.body)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertIsInstance(factory.call_args.kwargs["cookie_jar"], download.aiohttp.DummyCookieJar)
        self.assertEqual(client.get.call_args.args, (MEDIA,))
        self.assertFalse(client.get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("Cookie", client.get.call_args.kwargs["headers"])

    async def test_redirect_outside_platform_is_blocked_before_second_request(self):
        client = Mock()
        client.get.return_value = Response(code=302, headers={"Location": "http://127.0.0.1/private"})
        session = AsyncMock()
        session.__aenter__.return_value = client
        with patch.object(download.aiohttp, "ClientSession", return_value=session), \
             patch.object(download.aiohttp, "TCPConnector"), self.assertRaises(WebError):
            await download.save_resource({"urls": (MEDIA,)}, io.BytesIO())
        self.assertEqual(client.get.call_count, 1)

    async def test_empty_nonmedia_declared_and_streamed_size_limits(self):
        cases = [(Response(body=b""), "douyin_media_empty"),
                 (Response(headers={"Content-Type": "text/html"}), "douyin_media_content_type"),
                 (Response(headers={"Content-Length": str(download.MAX_DOWNLOAD + 1)}), "douyin_media_too_large")]
        for response, code in cases:
            client = Mock()
            client.get.return_value = response
            session = AsyncMock()
            session.__aenter__.return_value = client
            with patch.object(download.aiohttp, "ClientSession", return_value=session), \
                 patch.object(download.aiohttp, "TCPConnector"), self.assertRaisesRegex(WebError, code):
                await download.save_resource({"urls": (MEDIA,)}, io.BytesIO())
        client.get.return_value = Response(body=b"four")
        with patch.object(download.aiohttp, "ClientSession", return_value=session), \
             patch.object(download.aiohttp, "TCPConnector"), patch.object(download, "MAX_DOWNLOAD", 3), \
             self.assertRaisesRegex(WebError, "douyin_media_too_large"):
            await download.save_resource({"urls": (MEDIA,)}, io.BytesIO())


if __name__ == "__main__":
    unittest.main()

"""Offline tests for credentials, the real REST contract and transport boundaries."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import stat
import struct
import tempfile
import threading
import traceback
import unittest
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("xhs_core_tests", Path(__file__).with_name("core.py"))
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
SYNTHETIC_TOKEN = "synthetic-test-credential-only"


def envelope(data):
    return {"success": True, "data": data, "message": "ok"}


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff")) + chunk(b"IEND", b""))


class Response(io.BytesIO):
    def __init__(self, document=None, *, raw=None, status=200):
        super().__init__(raw if raw is not None else json.dumps(document).encode())
        self.status = status
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.token = Path(self.tmp.name) / "api-token"
        self.token.write_text(SYNTHETIC_TOKEN + "\n")
        self.token.chmod(0o600)
        self.requests = []

    def client(self, document=None, *, raw=None, status=200):
        def opener(request, **kwargs):
            self.requests.append(request)
            return Response(document, raw=raw, status=status)
        return core.XHSClient(endpoint="http://127.0.0.1:18060", token_file=str(self.token), opener=opener)

    def test_secure_file_modes_and_rotation(self):
        for mode in [0o600, 0o640]:
            self.token.chmod(mode)
            self.assertEqual(core._read_token(self.token), SYNTHETIC_TOKEN)
        self.token.write_text("rotated-synthetic-test-token")
        self.assertEqual(core._read_token(self.token), "rotated-synthetic-test-token")
        for mode in [0o400, 0o644, 0o660, 0o700, 0o4600]:
            self.token.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises(core.XHSError):
                core._read_token(self.token)

    def test_symlink_fifo_and_foreign_owner_rejected(self):
        linked = self.token.with_name("link")
        linked.symlink_to(self.token)
        with self.assertRaises(core.XHSError):
            core._read_token(linked)
        fifo = self.token.with_name("fifo")
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(core.XHSError):
            core._read_token(fifo)
        original = os.fstat
        def foreign(fd):
            data = original(fd)
            return SimpleNamespace(st_mode=data.st_mode, st_uid=max(os.geteuid(), 0) + 12345, st_size=data.st_size)
        with patch.object(core.os, "fstat", side_effect=foreign), self.assertRaises(core.XHSError):
            core._read_token(self.token)

    def test_file_is_not_reopened_after_descriptor_validation(self):
        original = os.open
        other = self.token.with_name("other")
        other.write_text("different-synthetic-token")
        other.chmod(0o600)
        def replace_after_open(path, flags):
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NONBLOCK)
            descriptor = original(path, flags)
            self.token.unlink()
            self.token.symlink_to(other)
            return descriptor
        with patch.object(core.os, "open", side_effect=replace_after_open):
            self.assertEqual(core._read_token(self.token), SYNTHETIC_TOKEN)

    def test_token_bounded_ascii_and_permissions(self):
        for content in ["x" * (core.MAX_TOKEN_LENGTH + 3), "short", "valid-token\r\nInjected: x", "非ASCIItoken123"]:
            self.token.write_text(content)
            with self.subTest(content=content[:12]), self.assertRaises(core.XHSError):
                core._read_token(self.token)
        self.token.write_text(SYNTHETIC_TOKEN)
        with patch.object(core.os, "read", return_value=b"x" * (core.MAX_TOKEN_LENGTH + 3)) as read:
            with self.assertRaises(core.XHSError):
                core._read_token(self.token)
        self.assertEqual(read.call_args.args[1], core.MAX_TOKEN_LENGTH + 3)

    def test_endpoint_rejects_credentials_paths_queries_and_nonloopback_http(self):
        for endpoint in ["http://localhost:18060", "http://example.invalid", "http://127.0.0.1/api", "http://127.0.0.1?", "https://example.invalid#", "https://user:pass@example.com", "https://example.invalid:444", "https://example.invalid:bad", "http://127.0.0.1:0", " http://127.0.0.1", "http://127.0.0.1\\@evil.invalid", "http://[::1"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(core.XHSError):
                core._endpoint(endpoint)
        self.assertEqual(core._endpoint("http://[::1]:18060/"), "http://[::1]:18060")
        self.assertEqual(core._endpoint("https://example.invalid/"), "https://example.invalid")

    def test_real_search_contract_and_only_normalized_fields(self):
        data = {"feeds": [
            {"id": "note123", "modelType": "note", "xsecToken": "discard-content-token", "url": "https://evil.invalid", "noteCard": {"displayTitle": "咖啡", "user": {"nickname": "作者"}}},
            {"id": "note123", "noteCard": {"displayTitle": "duplicate"}},
            {"id": "live123", "modelType": "live_v2", "noteCard": {}},
            {"id": "../bad", "noteCard": {}},
            {"id": "note456", "noteCard": {"displayTitle": "第二篇", "user": {"nickName": "另一位"}}},
        ], "count": 5}
        result = self.client(envelope(data)).search(" 咖啡 ", limit=2)
        self.assertEqual(result, [
            {"title": "咖啡", "url": "https://www.xiaohongshu.com/explore/note123", "snippet": "作者"},
            {"title": "第二篇", "url": "https://www.xiaohongshu.com/explore/note456", "snippet": "另一位"},
        ])
        request = self.requests[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:18060/api/v1/feeds/search")
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {"keyword": "咖啡", "filters": {}})
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SYNTHETIC_TOKEN)
        self.assertEqual(self.client(envelope({"feeds": []})).search("nothing"), [])

    def test_failure_and_wrong_search_schema_never_become_empty_success(self):
        for document in [
            {"success": False, "data": {"feeds": []}},
            {"error": "denied", "code": "SEARCH_FEEDS_FAILED"},
            {"success": 1, "data": {"feeds": []}},
            {"success": "true", "data": {"feeds": []}},
            {"success": True, "error": "denied", "data": {"feeds": []}},
            envelope({}), envelope({"feeds": None}), envelope({"feeds": {}}), {"feeds": []},
        ]:
            with self.subTest(document=document), self.assertRaises(core.XHSError):
                self.client(document).search("coffee")
        with self.assertRaises(core.XHSError):
            self.client(envelope({"feeds": []})).search("x" * 501)
        self.assertEqual(self.requests[-1].data, b'{"keyword": "coffee", "filters": {}}')

    def test_only_four_method_path_pairs_accepted(self):
        client = self.client(envelope({}))
        for method, path, payload in [
            ("POST", "/api/v1/publish", {}), ("GET", "//evil.invalid/", None),
            ("GET", "/api/v1/login/cookies", None), ("DELETE", "/api/v1/login/status", None),
            ("POST", "/api/v1/feeds/search?x=1", {}),
            ("POST", "/api/v1/feeds/search", {"keyword": "x", "filters": {}, "url": "evil"}),
            ("GET", "/api/v1/login/status", {"x": 1}),
        ]:
            with self.subTest(path=path), self.assertRaises(core.XHSError):
                client._request(method, path, payload)
        self.assertEqual(self.requests, [])

    def test_bounded_reads_bad_json_and_http_errors_are_safe(self):
        for raw, status in [(b"x" * (core.MAX_RESPONSE_BYTES + 1), 200), (b"<html>", 200), (b"{}", 302), (b"\xff", 200)]:
            with self.subTest(status=status), self.assertRaises(core.XHSError):
                self.client(raw=raw, status=status).login_status()
        response = Response(envelope({"is_logged_in": True}))
        client = core.XHSClient(endpoint="http://127.0.0.1", token_file=str(self.token), opener=lambda *_a, **_k: response)
        self.assertTrue(client.login_status())
        self.assertEqual(response.read_sizes, [core.MAX_RESPONSE_BYTES + 1])
        for failure in [urllib.error.HTTPError("https://invalid/" + SYNTHETIC_TOKEN, 401, SYNTHETIC_TOKEN, {}, None), RuntimeError(SYNTHETIC_TOKEN)]:
            def opener(*_a, **_k):
                raise failure
            client._opener = opener
            try:
                client.login_status()
            except core.XHSError as error:
                rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                self.assertNotIn(SYNTHETIC_TOKEN, rendered)
                self.assertIsNone(error.__cause__)
            else:
                self.fail("expected safe failure")

    def test_upstream_echoes_are_discarded(self):
        for data in [{"feeds": [{"id": "n", "noteCard": {"displayTitle": SYNTHETIC_TOKEN}}]}, {"is_logged_in": True, "message": SYNTHETIC_TOKEN}]:
            client = self.client(envelope(data))
            with self.assertRaises(core.XHSError) as raised:
                client._request("GET", "/api/v1/login/status")
            self.assertNotIn(SYNTHETIC_TOKEN, str(raised.exception))
        quoted_token = 'synthetic"quoted\\token'
        self.token.write_text(quoted_token)
        with self.assertRaises(core.XHSError):
            self.client(envelope({"is_logged_in": True, "echo": quoted_token})).login_status()
        self.token.write_text(SYNTHETIC_TOKEN)
        encoded = "".join("\\u%04x" % ord(c) for c in SYNTHETIC_TOKEN)
        with self.assertRaises(core.XHSError):
            self.client(raw=('{"success":true,"data":{"is_logged_in":true,"echo":"' + encoded + '"}}').encode()).login_status()

    def test_status_requires_boolean_and_logout_requires_success(self):
        for value in [True, False]:
            self.assertIs(self.client(envelope({"is_logged_in": value})).login_status(), value)
        for value in ["false", 0, 1, None, []]:
            with self.subTest(value=value), self.assertRaises(core.XHSError):
                self.client(envelope({"is_logged_in": value})).login_status()
        self.client(envelope({"cookie_path": "discarded"})).logout()
        self.assertEqual(self.requests[-1].method, "DELETE")
        with self.assertRaises(core.XHSError):
            self.client({"success": False}).logout()

    def test_qr_valid_png_and_rejects_malformed_or_huge_images(self):
        value = png()
        data = {"is_logged_in": False, "timeout": "4m0s", "img": "data:image/png;base64," + base64.b64encode(value).decode()}
        self.assertEqual(self.client(envelope(data)).login_qr(), value)
        invalid = ["data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,%%%", "data:image/png;base64," + base64.b64encode(b"not a png").decode(), "data:image/png;base64," + "A" * (core.MAX_QR_BYTES * 2), "data:image/png;base64," + base64.b64encode(value[:-1] + b"x").decode()]
        for image in invalid:
            with self.subTest(length=len(image)), self.assertRaises(core.XHSError):
                self.client(envelope({"is_logged_in": False, "img": image})).login_qr()
        with self.assertRaises(core.XHSError) as raised:
            self.client(envelope({"is_logged_in": True})).login_qr()
        self.assertEqual(raised.exception.code, "already_logged_in")

    def test_default_transport_never_follows_redirect_or_uses_proxy(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                if self.path == "/api/v1/login/status":
                    self.send_response(307)
                    self.send_header("Location", "/credential-leak-target")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(envelope({"is_logged_in": True})).encode())
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1", "no_proxy": ""}), patch("urllib.request.proxy_bypass", return_value=False):
                client = core.XHSClient(endpoint=f"http://127.0.0.1:{server.server_port}", token_file=str(self.token))
                with self.assertRaises(core.XHSError):
                    client.login_status()
            self.assertEqual(received, ["/api/v1/login/status"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

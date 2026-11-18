"""Douyin source/transport boundaries without network, browser, model or QQ calls."""
import asyncio
import inspect
import json
import socket
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import douyin
import douyin_browser as browser
import douyin_egress as egress
from public_web import WebError


VIDEO_ID = "7123456789012345678"
PUBLIC_IP = ".".join(map(str, (93, 184, 216, 34)))
PAGE = "https://www.douyin.com/video/" + VIDEO_ID
MEDIA = "https://v26-web.douyinvod.com/video/example/?x-signature=media-signature"


def detail():
    return {"aweme_id": VIDEO_ID, "desc": "test video", "authentication_token": "synthetic-private-value",
            "unknown": {"future_field": [1, "preserved"]},
            "chapter_list": [{"title": "AI chapter, not a transcript"}],
            "video": {"duration": 330767, "format": "dash", "bit_rate": [
                {"is_h265": 0, "format": "dash", "gear_name": "low", "bit_rate": 800000,
                 "play_addr": {"url_list": [MEDIA], "data_size": 33000000, "width": 720, "height": 1280}}],
                "bit_rate_audio": [{"audio_meta": {"url_list": ["https://v26-web.douyinvod.com/audio/example/"],
                                                    "size": 1000000}}]}}


class DouyinTests(unittest.TestCase):
    def test_short_and_canonical_urls_drop_tracking(self):
        self.assertEqual(douyin.page_url("https://v.douyin.com/AbC123/?share_sign=synthetic#ignored"),
                         ("https://v.douyin.com/AbC123/", None))
        for host, path in (("www.douyin.com", "/video/"), ("douyin.com", "/video/"),
                           ("www.iesdouyin.com", "/share/video/"), ("m.douyin.com", "/share/video/")):
            self.assertEqual(douyin.page_url("https://" + host + path + VIDEO_ID + "/?utm_source=share"), (PAGE, VIDEO_ID))

    def test_invalid_input_never_becomes_a_browser_target(self):
        values = [None, "", "看看 " + PAGE, "file:///etc/passwd", "https://127.0.0.1/video/" + VIDEO_ID,
                  "https://www.douyin.com.evil.com/video/" + VIDEO_ID, "https://douyin.com:8443/video/" + VIDEO_ID,
                  "https://user:secret" + "@" + "www.douyin.com/video/" + VIDEO_ID, "https://v.douyin.com/../../",
                  "https://www.douyin.com/user/123", "https://www.douyin.com/video/123", PAGE + "\nheader"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(WebError):
                douyin.page_url(value)

    def test_resource_hosts_and_ports_are_constrained_but_media_signatures_survive(self):
        self.assertEqual(douyin.resource_url(MEDIA), MEDIA)
        for value in ("http://localhost/a", "http://" + ".".join(["169", "254"] * 2) + "/", "https://douyinvod.com.evil.com/a",
                      "https://user:secret" + "@" + "www.douyin.com/a", "https://www.douyin.com:9000/a", "data:test",
                      "https://www.douyin.com/\nheader", "https://example.com/a", None):
            with self.subTest(value=value), self.assertRaises(WebError):
                douyin.resource_url(value)

    def test_only_ephemeral_bootstrap_posts_and_read_requests_are_allowed(self):
        self.assertTrue(douyin.allowed_request(PAGE, "GET", "document", None))
        self.assertTrue(douyin.allowed_request("https://mssdk.bytedance.com/web/r/token", "POST", "xhr", b"anonymous"))
        for url, method, kind, body in [
            (PAGE, "DELETE", "xhr", None), (PAGE, "POST", "xhr", b"data"),
            ("https://www.douyin.com/aweme/v1/web/commit/", "GET", "xhr", None),
            ("https://www.douyin.com/passport/login/", "GET", "xhr", None),
            ("https://mcs.zijieapi.com/list", "POST", "xhr", b"tracking"),
            ("https://mssdk.bytedance.com/web/r/token", "POST", "xhr", b"a" * 65537),
            (MEDIA, "GET", "media", None), (MEDIA, "GET", "image", None),
            (MEDIA, "GET", "fetch", None),
            (PAGE, "GET", "websocket", None), (PAGE, "GET", "xhr", b"body"),
        ]:
            with self.subTest(url=url, method=method, kind=kind):
                self.assertFalse(douyin.allowed_request(url, method, kind, body))

    def test_original_detail_preserved_and_credentials_removed_before_summary(self):
        source = detail()
        source["desc"] = "echo synthetic-private-value"
        source["url"] = "https://www.douyin.com/?access_token=private-query&item=1"
        source["nested"] = {"Cookie": "private-cookie", "echo": "private-cookie"}
        result = douyin.describe_detail(source, VIDEO_ID)
        serialized = json.dumps(result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration_seconds"], 330.767)
        self.assertEqual(result["source"]["unknown"], source["unknown"])
        self.assertEqual(result["source"]["chapter_list"], source["chapter_list"])
        self.assertEqual(result["subtitles"], [])
        self.assertEqual([item["kind"] for item in result["media"]], ["video", "audio"])
        self.assertEqual(result["media"][0]["urls"], [MEDIA])
        for secret in ("authentication_token", "synthetic-private-value", "private-cookie", "private-query"):
            self.assertNotIn(secret, serialized)
        self.assertIn("not a viewing result", result["notice"])
        self.assertIn("platform-generated", result["notice"])
        self.assertIn("authentication_token", source)

    def test_mismatch_and_metadata_only_do_not_claim_video_success(self):
        with self.assertRaisesRegex(WebError, "douyin_video_id_mismatch"):
            douyin.describe_detail(detail(), "7999999999999999999")
        source = detail()
        source["video"] = {"duration": 3000, "cover": {"url_list": [MEDIA]}}
        with self.assertRaisesRegex(WebError, "douyin_video_resources_unavailable"):
            douyin.describe_detail(source, VIDEO_ID)
        source = detail()
        source["video"]["bit_rate"] = None
        with self.assertRaisesRegex(WebError, "douyin_video_resources_unavailable"):
            douyin.describe_detail(source, VIDEO_ID)

    def test_malformed_optional_media_fields_do_not_crash(self):
        source = detail()
        source["video"]["bit_rate_audio"] = 123
        source["video"]["subtitle_infos"] = {"unexpected": True}
        self.assertTrue(douyin.describe_detail(source, VIDEO_ID)["ok"])

    def test_unsafe_or_duplicate_media_is_not_advertised(self):
        source = detail()
        source["video"]["play_addr"] = {"url_list": [MEDIA, "https://localhost/a", "https://evil.com/a"]}
        result = douyin.describe_detail(source, VIDEO_ID)
        self.assertEqual(len(result["media"]), 2)

    def test_complex_and_nonfinite_source_fails_closed(self):
        for invalid in ({"value": float("nan")}, {"value": float("inf")}, ["not-a-dict"]):
            with self.assertRaises(WebError):
                douyin.sanitized_detail(invalid)
        nested = {}
        for _ in range(55):
            nested = {"value": nested}
        with self.assertRaisesRegex(WebError, "douyin_detail_too_complex"):
            douyin.sanitized_detail(nested)

    def test_login_verification_and_missing_video_are_distinct_from_authorization(self):
        self.assertEqual(douyin.unavailable_error("登录后观看"), "douyin_login_or_private_video")
        self.assertEqual(douyin.unavailable_error("请完成验证 拖动滑块"), "douyin_verification_required")
        self.assertEqual(douyin.unavailable_error("作品已删除"), "douyin_video_not_found")
        self.assertEqual(douyin.unavailable_error("导航 登录 推荐"), "douyin_video_resources_unavailable")

    def test_api_capture_requires_matching_request_and_response_ids(self):
        transport = browser.Transport(None, VIDEO_ID)
        url = "https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + VIDEO_ID
        body = json.dumps({"aweme_detail": detail()}).encode()
        for wrong in (url.replace(VIDEO_ID, "7999999999999999999"), url.replace("www.douyin.com", "evil.com")):
            transport.capture(wrong, 200, body)
            self.assertFalse(transport.ready.is_set())
        transport.capture(url, 403, body)
        transport.capture(url, 200, b"not-json")
        self.assertFalse(transport.ready.is_set())
        transport.capture(url, 200, body)
        self.assertTrue(transport.ready.is_set())
        self.assertEqual(transport.source["aweme_id"], VIDEO_ID)

    def test_api_capture_rejects_oversized_data(self):
        transport = browser.Transport(None, VIDEO_ID)
        with patch.object(douyin, "MAX_DETAIL", 4), self.assertRaisesRegex(WebError, "douyin_response_too_large"):
            transport.capture("https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=" + VIDEO_ID, 200, b"too large")


class DouyinAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_does_not_open_browser_for_invalid_input(self):
        with patch.object(browser, "read_source", new_callable=AsyncMock) as read:
            result = await douyin.read_douyin_video("https://private.local/a")
            self.assertFalse(result["ok"])
            read.assert_not_called()

    async def test_tool_returns_source_and_reports_stable_failures(self):
        with patch.object(browser, "read_source", new_callable=AsyncMock, return_value=(detail(), VIDEO_ID)):
            self.assertTrue((await douyin.read_douyin_video(PAGE))["ok"])
        for failure, code in ((WebError("douyin_verification_required"), "douyin_verification_required"),
                              (TimeoutError("private diagnostics"), "douyin_read_timeout")):
            with patch.object(browser, "read_source", new_callable=AsyncMock, side_effect=failure):
                self.assertEqual(await douyin.read_douyin_video(PAGE), {"ok": False, "error": code})

    async def test_share_redirects_never_contact_unrelated_or_private_sites(self):
        for location in ("http://127.0.0.1/", "https://example.com/a", "https://www.douyin.com/user/123"):
            transport = browser.Transport(None)
            transport.fetch = AsyncMock(return_value=(302, {"Location": location}, b""))
            with self.assertRaises(WebError):
                await transport.resolve_share("https://v.douyin.com/AbC123/")
            self.assertEqual(transport.fetch.await_count, 1)

    async def test_short_link_resolves_to_clean_video_url_and_redirects_are_bounded(self):
        transport = browser.Transport(None)
        transport.fetch = AsyncMock(return_value=(302, {"Location": "https://www.iesdouyin.com/share/video/" + VIDEO_ID + "/?share_sign=private"}, b""))
        self.assertEqual(await transport.resolve_share("https://v.douyin.com/AbC123/"), (PAGE, VIDEO_ID))
        transport.fetch = AsyncMock(return_value=(302, {"Location": "https://v.douyin.com/AbC123/"}, b""))
        with self.assertRaisesRegex(WebError, "douyin_redirect_limit"):
            await transport.resolve_share("https://v.douyin.com/AbC123/")
        self.assertEqual(transport.fetch.await_count, 5)

    async def test_public_resolver_pins_only_verified_addresses(self):
        record = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (PUBLIC_IP, 443))
        with patch.object(browser, "public_addresses", return_value=[record]) as addresses:
            result = await browser.PublicResolver().resolve("www.douyin.com", 443)
            addresses.assert_called_once_with("www.douyin.com", 443)
            self.assertEqual(result[0]["host"], PUBLIC_IP)
            self.assertEqual(result[0]["flags"], socket.AI_NUMERICHOST)
        with patch.object(browser, "public_addresses", side_effect=WebError("private_address_blocked")):
            with self.assertRaisesRegex(WebError, "private_address_blocked"):
                await browser.PublicResolver().resolve("www.douyin.com", 443)

    async def test_request_budget_stops_before_network(self):
        transport = browser.Transport(Mock())
        transport.requests = douyin.MAX_REQUESTS
        with self.assertRaisesRegex(WebError, "douyin_transfer_limit"):
            await transport.fetch(PAGE)
        transport.client.request.assert_not_called()

    async def test_forbidden_browser_request_is_aborted_without_fetch(self):
        transport = browser.Transport(None)
        transport.fetch = AsyncMock()
        route = Mock()
        route.request.url, route.request.method = "https://localhost/private", "GET"
        route.request.resource_type, route.request.post_data_buffer = "xhr", None
        route.abort = AsyncMock()
        await transport.route(route)
        route.abort.assert_awaited_once()
        transport.fetch.assert_not_called()

    async def test_allowed_browser_requests_keep_native_tls_but_obey_budget(self):
        transport = browser.Transport(None)
        route = Mock()
        route.request.url, route.request.method = PAGE, "GET"
        route.request.resource_type, route.request.post_data_buffer = "document", None
        route.abort, route.continue_ = AsyncMock(), AsyncMock()
        await transport.route(route)
        route.continue_.assert_awaited_once()
        transport.requests = douyin.MAX_REQUESTS
        await transport.route(route)
        route.abort.assert_awaited_once()
        self.assertEqual(transport.failure, "douyin_transfer_limit")

    async def test_egress_rejects_nonplatform_hosts_and_private_dns_before_connection(self):
        connection = egress.PublicEgress()
        with patch.object(egress, "public_addresses") as addresses:
            for host, port in (("127.0.0.1", 443), ("localhost", 443), ("example.com", 443), ("www.douyin.com", 9000)):
                with self.assertRaises(WebError):
                    await connection.connect(host, port)
            addresses.assert_not_called()
        with patch.object(egress, "public_addresses", side_effect=WebError("private_address_blocked")), \
             patch.object(egress.socket, "socket") as raw_socket:
            with self.assertRaisesRegex(WebError, "private_address_blocked"):
                await connection.connect("www.douyin.com", 443)
            raw_socket.assert_not_called()

    async def test_egress_connects_to_pinned_numeric_address_without_second_dns_lookup(self):
        connection = egress.PublicEgress()
        record = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, (PUBLIC_IP, 443))
        raw_socket = Mock()
        loop = asyncio.get_running_loop()
        with patch.object(egress, "public_addresses", return_value=[record]) as addresses, \
             patch.object(egress.socket, "socket", return_value=raw_socket), \
             patch.object(loop, "sock_connect", new_callable=AsyncMock) as connect, \
             patch.object(egress.asyncio, "open_connection", new_callable=AsyncMock, return_value=("reader", "writer")) as streams:
            self.assertEqual(await connection.connect("www.douyin.com", 443), ("reader", "writer"))
            addresses.assert_called_once_with("www.douyin.com", 443)
            connect.assert_awaited_once_with(raw_socket, (PUBLIC_IP, 443))
            streams.assert_awaited_once_with(sock=raw_socket)
            await connection.connect("www.douyin.com", 443)
            self.assertEqual(addresses.call_count, 1)

    async def test_egress_byte_budget_and_cancellation_close_connections(self):
        connection = egress.PublicEgress()
        reader, writer = Mock(), Mock()
        reader.read = AsyncMock(side_effect=[b"abcd", b""])
        writer.drain = AsyncMock()
        with patch.object(douyin, "MAX_TRANSFER", 3), self.assertRaisesRegex(WebError, "douyin_transfer_limit"):
            await connection.relay(reader, writer)
        writer.write.assert_not_called()
        self.assertEqual(connection.failure, "douyin_transfer_limit")

    async def test_egress_rejects_ip_literal_socks_requests_and_closes_client(self):
        connection = egress.PublicEgress()
        reader, writer = Mock(), Mock()
        reader.readexactly = AsyncMock(side_effect=[b"\x05\x01", b"\x00", b"\x05\x01\x00\x01"])
        writer.drain, writer.wait_closed = AsyncMock(), AsyncMock()
        connection.connect = AsyncMock()
        await connection.accept(reader, writer)
        connection.connect.assert_not_called()
        writer.close.assert_called_once()
        self.assertFalse(connection.tasks)

    async def test_single_browser_lock_is_released_after_failure_and_busy_is_stable(self):
        import fcntl
        with tempfile.NamedTemporaryFile() as lockfile, patch.object(browser, "__file__", lockfile.name):
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(WebError, "douyin_browser_busy"):
                await browser.read_source(PAGE, VIDEO_ID)
            fcntl.flock(lockfile, fcntl.LOCK_UN)
            with patch.object(browser, "_read_source", new_callable=AsyncMock, side_effect=WebError("synthetic_failure")):
                with self.assertRaisesRegex(WebError, "synthetic_failure"):
                    await browser.read_source(PAGE, VIDEO_ID)
            fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lockfile, fcntl.LOCK_UN)

    async def test_mcp_catalog_has_readonly_douyin_tool_with_no_credential_parameters(self):
        import public_web_mcp
        tools = {tool.name: tool for tool in await public_web_mcp.server.list_tools()}
        tool = tools["read_douyin_video"]
        self.assertTrue(tool.annotations.readOnlyHint)
        self.assertEqual(list(tool.inputSchema["properties"]), ["url"])
        self.assertEqual(list(inspect.signature(douyin.read_douyin_video).parameters), ["url"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image as PillowImage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xhs_search.core import XHSClient, XHSError
from xhs_search.notes import (ReferenceStore, download_image, image_url, normalize_note, search_references,
                              strip_markdown_images, validate_image)


URL = "https://sns-webpic-qc.xhscdn.com/synthetic-image"
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93." + "184." + "216." + "34", 443))]


def picture():
    output = io.BytesIO()
    PillowImage.new("RGB", (20, 15), "#119966").save(output, format="PNG")
    return output.getvalue()


class NotesTests(unittest.TestCase):
    def test_only_exact_cdn_hosts_https_and_no_userinfo(self):
        self.assertEqual(image_url(URL.replace("https:", "http:")), URL)
        for url in ("http://127.0.0.1/image", "https://sns-webpic-qc.xhscdn.com.evil.invalid/a", "file:///etc/passwd",
                    "https://user:secret" + "@" + "sns-webpic-qc.xhscdn.com/a", "https://sns-webpic-qc.xhscdn.com:8080/a",
                    "https://sns-webpic-qc.xhscdn.com/a#token", "https://sns-webpic-qc.xhscdn.com/a\nHeader"):
            with self.subTest(url=url), self.assertRaises(XHSError):
                image_url(url)

    def test_details_have_body_original_images_no_access_token_or_comments(self):
        data = {"feed_id": "note-one", "data": {"note": {"noteId": "note-one", "title": "原帖", "desc": "原帖正文",
                "xsecToken": "synthetic-private-access", "user": {"nickname": "作者"}, "imageList": [
                    {"urlDefault": URL}, {"urlDefault": "https://evil.invalid/private"}], "type": "normal"},
                "comments": {"private": "not-needed"}}}
        note = normalize_note(data, "note-one")
        self.assertEqual(note["body"], "原帖正文")
        self.assertEqual(note["image_urls"], [URL])
        self.assertNotIn("synthetic-private-access", json.dumps(note))
        self.assertNotIn("comments", note)
        with self.assertRaises(XHSError):
            normalize_note(data, "foreign-note")

    def test_references_preserve_access_parameters_only_for_normalized_ids(self):
        references = search_references({"feeds": [{"id": "one", "xsecToken": "private-access"}, {"id": "two", "xsecToken": "other"}]},
                                       [{"url": "https://www.xiaohongshu.com/explore/one"}])
        self.assertEqual(set(references), {"one"})
        self.assertEqual(references["one"]["xsec_token"], "private-access")

    def test_cache_is_both_conversation_and_sender_scoped(self):
        clock = [100]
        store = ReferenceStore(clock=lambda: clock[0])
        store.put(("group", "owner"), "桌游", {"one": {"xsec_token": "private"}})
        self.assertEqual(store.get(("group", "owner"))["query"], "桌游")
        for owner in (("other-group", "owner"), ("group", "other")):
            with self.assertRaises(XHSError):
                store.get(owner)
        clock[0] += 1801
        with self.assertRaises(XHSError):
            store.get(("group", "owner"))

    def test_bounded_cache_removes_oldest_owner(self):
        store = ReferenceStore()
        for index in range(45):
            store.put(("group", str(index)), "query", {})
        self.assertEqual(len(store.records), 40)
        with self.assertRaises(XHSError):
            store.get(("group", "0"))

    def test_validate_reencodes_pixels_to_jpeg(self):
        verified = validate_image(picture())
        self.assertEqual((verified.width, verified.height, verified.mime_type), (20, 15, "image/jpeg"))
        self.assertTrue(verified.data.startswith(b"\xff\xd8"))
        for raw in (b"<html>error</html>", b"", b"x" * (5 * 1024 * 1024 + 1)):
            with self.assertRaises(XHSError):
                validate_image(raw)

    def test_parenthesized_fake_markdown_urls_are_removed_completely(self):
        text = "玩法\n![游戏](https://cf.geekdo-images.com/synthetic/filters:strip_icc()/pic.jpg)\n来源"
        self.assertEqual(strip_markdown_images(text), "玩法\n\n来源")
        self.assertEqual(strip_markdown_images("普通文字"), "普通文字")

    def test_detail_payload_disallows_arbitrary_endpoints_comments_and_credentials_in_query(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "token"
            token.write_text("synthetic-server-api-secret")
            token.chmod(0o600)
            opener = unittest.mock.Mock()
            client = XHSClient(endpoint="http://127.0.0.1:18060", token_file=str(token), opener=opener)
            for payload in ({"feed_id": "one", "xsec_token": "private", "load_all_comments": True},
                            {"feed_id": "one", "xsec_token": "private", "load_all_comments": False, "url": "http://internal"},
                            {"feed_id": "one", "xsec_token": "private\nHeader", "load_all_comments": False}):
                with self.assertRaises(XHSError):
                    client._request("POST", "/api/v1/feeds/detail", payload)
            opener.assert_not_called()


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, handler):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_verified_picture_no_bearer_cookie_or_login_info_forwarded(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, content=picture(), headers={"Content-Type": "image/png"})
        client = await self.client(handler)
        result = await download_image(URL, client=client, resolver=lambda *args, **kwargs: PUBLIC)
        self.assertEqual(result.width, 20)
        self.assertNotIn("authorization", requests[0].headers)
        self.assertNotIn("cookie", requests[0].headers)

    async def test_private_dns_rejected_before_http(self):
        handler = unittest.mock.Mock()
        client = await self.client(handler)
        with self.assertRaises(XHSError):
            await download_image(URL, client=client, resolver=lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
        handler.assert_not_called()

    async def test_429_no_retry_and_no_outlet_switch(self):
        requests = []
        client = await self.client(lambda request: requests.append(request) or httpx.Response(429))
        with self.assertRaises(XHSError) as error:
            await download_image(URL, client=client, resolver=lambda *args, **kwargs: PUBLIC)
        self.assertEqual(error.exception.code, "image_restricted")
        self.assertEqual(len(requests), 1)

    async def test_redirect_cannot_escape_cdn_allowlist(self):
        requests = []
        client = await self.client(lambda request: requests.append(request) or httpx.Response(302, headers={"Location": "http://127.0.0.1/private"}))
        with self.assertRaises(XHSError):
            await download_image(URL, client=client, resolver=lambda *args, **kwargs: PUBLIC)
        self.assertEqual(len(requests), 1)

    async def test_html_and_oversized_responses_not_images(self):
        for response in (httpx.Response(200, content=b"<html/>", headers={"Content-Type": "text/html"}),
                         httpx.Response(200, content=b"x", headers={"Content-Type": "image/png", "Content-Length": str(6000000)})):
            client = await self.client(lambda request: response)
            with self.assertRaises(XHSError):
                await download_image(URL, client=client, resolver=lambda *args, **kwargs: PUBLIC)

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from xhs_search.core import XHSClient, XHSError
from xhs_search.source import sanitized_source


def detail():
    return {"feed_id": "synthetic-note", "data": {"note": {
        "noteId": "synthetic-note", "title": "Synthetic video", "desc": "Full source text", "type": "video",
        "xsecToken": "synthetic-private-reference", "imageList": [], "user": {"nickname": "Synthetic author"},
        "video": {"media": {"stream": {"future-codec": [{"masterUrl": "https://video.example.com/note.mp4?sign=resource-signature",
                  "width": 720, "height": 1280, "duration": 140733}]}},
                  "subtitles": {"source": "zh-CN", "zh-CN": [{"url": "https://video.example.com/subtitle.vtt", "future_field": [1, 2]}]}},
        "unknown_upstream_field": {"nested": [False, None, 5]}},
        "comments": {"has_more": True, "list": [{"content": "Synthetic public comment"}]}}}


class SourceTests(unittest.TestCase):
    def test_source_preserves_all_content_fields_without_interpreting_media(self):
        original = detail()
        before = copy.deepcopy(original)
        result = sanitized_source(original)
        expected = copy.deepcopy(original)
        del expected["data"]["note"]["xsecToken"]
        self.assertEqual(result, expected)
        self.assertEqual(original, before)
        result["data"]["note"]["video"]["subtitles"]["zh-CN"].clear()
        self.assertEqual(original, before)

    def test_nested_credentials_references_and_encoded_echoes_are_removed(self):
        secret = "synthetic+" + "private/reference="
        result = sanitized_source({"xsecToken": secret, "nested": [{"Cookie": "synthetic-cookie", "api_key": "synthetic-key"}],
            "echo": "copied " + secret + " " + quote(secret, safe=""),
            "url": "https://www.xiaohongshu.com/explore/note?xsec_token=" + quote(secret, safe="") + "&keep=1",
            "bad": "https://user:password@" + "private.example.com/resource",
            "key-" + secret: "safe"})
        encoded = json.dumps(result)
        for value in (secret, quote(secret, safe=""), "synthetic-cookie", "synthetic-key", "xsec_token", "password@"):
            self.assertNotIn(value, encoded)
        self.assertEqual(result["url"], "https://www.xiaohongshu.com/explore/note?keep=1")

    def test_short_private_values_are_not_leaked_in_larger_text(self):
        self.assertNotIn("xyz", json.dumps(sanitized_source({"echo": "prefixxyzsuffix"}, private_values=["xyz"])))

    def test_invalid_or_excessively_deep_structures_fail_without_partial_source(self):
        nested = {}
        for unused in range(66):
            nested = {"child": nested}
        for value in (nested, {"value": float("nan")}, {"value": float("inf")}):
            with self.subTest(value=type(value).__name__), self.assertRaises(XHSError):
                sanitized_source(value)

    def test_client_only_exposes_source_when_trusted_caller_requests_it(self):
        response = detail()
        client = XHSClient(endpoint="http://127.0.0.1:18060", token_file="unused")
        client._request = Mock(return_value=response)
        ordinary = client.note_detail("synthetic-note", "synthetic-private-reference")
        trusted = client.note_detail("synthetic-note", "synthetic-private-reference", include_source=True)
        self.assertNotIn("source", ordinary)
        self.assertEqual(trusted["source"]["data"]["note"]["video"], response["data"]["note"]["video"])
        self.assertNotIn("synthetic-private-reference", json.dumps(trusted))
        client._request.assert_called_with("POST", "/api/v1/feeds/detail", {
            "feed_id": "synthetic-note", "xsec_token": "synthetic-private-reference", "load_all_comments": False})
        with self.assertRaises(XHSError):
            client.note_detail("foreign-note", "synthetic-private-reference", include_source=True)

    def test_trusted_summary_fields_are_sanitized_together_with_source(self):
        response = detail()
        response["data"]["note"]["desc"] = "echo synthetic-private-reference"
        response["data"]["note"]["title"] = "synthetic-private-reference"
        client = XHSClient(endpoint="http://127.0.0.1:18060", token_file="unused")
        client._request = Mock(return_value=response)
        result = client.note_detail("synthetic-note", "synthetic-private-reference", include_source=True)
        self.assertNotIn("synthetic-private-reference", json.dumps(result))

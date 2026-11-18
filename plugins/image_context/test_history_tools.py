"""History adapters retain native vision and never expose storage internals."""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("image_history_tools_tests", Path(__file__).with_name("history_tools.py"))
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
IMAGE_ID = "a" * 64
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"synthetic-pixels").decode()
SECRET = "/private/chat/secret-image.jpg?token=private-credential"


class FunctionTool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Plugin:
    def __init__(self):
        self.calls = []
        self.allowed = True
        self.list_result = {"ok": True, "images": [{
            "image_id": IMAGE_ID, "received_wall_at": 100.5, "source": "upload", "name": "图片1.jpg",
            "path": SECRET, "url": SECRET, "sender": "private-sender", "umo": "private-chat",
            "message_id": "private-message", "caption": "untrusted-caption",
        }], "has_more": True, "next_before": 100.5, "next_cursor": "7:100.5", "private_path": SECRET}
        self.read_result = {"ok": True, "mime": "image/png", "data": PNG, "private_path": SECRET}

    def _allowed(self, event):
        return self.allowed and event.scope == "original-chat"

    def list_history_for_task(self, event, **arguments):
        self.calls.append(("list", event, arguments))
        return self.list_result

    async def read_history_for_task(self, event, image_id):
        self.calls.append(("read", event, image_id))
        return self.read_result


class HistoryToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = Plugin()
        self.event = types.SimpleNamespace(scope="original-chat", is_at_or_wake_command=True,
                                           text="能估算楼层么")
        module = types.ModuleType("astrbot.core.agent.tool")
        module.FunctionTool = FunctionTool
        with patch.dict(sys.modules, {module.__name__: module}):
            self.tools = {tool.name: tool for tool in history.make_tools(self.plugin)}

    async def test_keywordless_question_lists_real_images_with_exact_event(self):
        result = json.loads(await self.tools["list_current_chat_images"].handler(self.event, limit=2, before=200))
        self.assertEqual(result["images"], [{"image_id": IMAGE_ID, "received_wall_at": 100.5,
                                             "source": "upload", "name": "图片1.jpg"}])
        self.assertEqual(result["next_before"], 100.5)
        self.assertEqual(result["next_cursor"], "7:100.5")
        self.assertTrue(result["has_more"])
        self.assertIs(self.plugin.calls[0][1], self.event)
        self.assertEqual(self.plugin.calls[0][2], {"limit": 2, "before": 200})
        rendered = json.dumps(result)
        for sensitive in (SECRET, "private-sender", "private-chat", "private-message", "untrusted-caption"):
            self.assertNotIn(sensitive, rendered)

    async def test_revoked_unaddressed_and_different_chat_do_not_invoke_backend(self):
        for mutation in (lambda: setattr(self.plugin, "allowed", False),
                         lambda: setattr(self.event, "is_at_or_wake_command", False),
                         lambda: setattr(self.event, "scope", "different-chat")):
            self.setUp()
            mutation()
            result = json.loads(await self.tools["list_current_chat_images"].handler(self.event))
            self.assertEqual(result["code"], "not_authorized")
            self.assertEqual(self.plugin.calls, [])

    async def test_invalid_pagination_and_reserved_identity_arguments_rejected(self):
        for arguments in ({"limit": True}, {"limit": 0}, {"limit": 13}, {"limit": 1.5},
                          {"before": True}, {"before": 0}, {"before": float("nan")},
                          {"before": float("inf")}, {"before": "123"}, {"before": 10 ** 1000},
                          {"cursor": "../private"}, {"cursor": "1:nan"}, {"cursor": "1:inf"},
                          {"cursor": "0:100.5"}, {"cursor": "7:-100.5"}, {"cursor": "7:1e1000"},
                          {"cursor": "9223372036854775808:100.5"}, {"cursor": "7:100.5\n"},
                          {"cursor": "7:" + "1" * 95}, {"cursor": False}):
            result = json.loads(await self.tools["list_current_chat_images"].handler(self.event, **arguments))
            self.assertEqual(result["code"], "invalid_arguments")
        for name in ("sender", "umo", "path", "url", "owner"):
            with self.assertRaises(TypeError):
                await self.tools["list_current_chat_images"].handler(self.event, **{name: SECRET})
            for tool in self.tools.values():
                self.assertNotIn(name, tool.parameters["properties"])
                self.assertFalse(tool.parameters["additionalProperties"])
        self.assertEqual(self.plugin.calls, [])

    async def test_stable_cursor_is_forwarded_and_backend_cursor_is_returned(self):
        self.plugin.list_result["next_cursor"] = "5:100.5"
        result = json.loads(await self.tools["list_current_chat_images"].handler(self.event, limit=3, cursor="7:100.5"))
        self.assertEqual(self.plugin.calls[0][2], {"limit": 3, "before": None, "cursor": "7:100.5"})
        self.assertEqual(result["next_cursor"], "5:100.5")
        self.assertEqual(result["next_before"], 100.5)
        self.assertNotEqual(result["next_cursor"], "7:100.5")

    async def test_backend_cursor_never_leaks_a_private_path_and_is_not_synthesized(self):
        self.plugin.list_result["next_cursor"] = SECRET
        result = json.loads(await self.tools["list_current_chat_images"].handler(self.event))
        self.assertEqual(result["code"], "invalid_result")
        self.assertNotIn(SECRET, json.dumps(result))
        self.plugin.list_result.pop("next_cursor")
        self.plugin.list_result.pop("next_before")
        result = json.loads(await self.tools["list_current_chat_images"].handler(self.event))
        self.assertNotIn("next_cursor", result)
        self.assertNotIn("next_before", result)

    async def test_unsafe_names_and_provider_error_details_are_not_exposed(self):
        self.plugin.list_result["images"][0]["name"] = SECRET
        result = json.loads(await self.tools["list_current_chat_images"].handler(self.event))
        self.assertNotIn("name", result["images"][0])
        self.plugin.list_result = {"ok": False, "error": SECRET}
        result = await self.tools["list_current_chat_images"].handler(self.event)
        self.assertNotIn(SECRET, result)

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_read_returns_native_mcp_image_and_exact_event(self):
        response = await self.tools["read_current_chat_image"].handler(self.event, IMAGE_ID)
        result = response.model_dump(mode="json", exclude_none=True, by_alias=True)
        self.assertFalse(result["isError"])
        self.assertEqual([block["type"] for block in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["data"], PNG)
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertIs(self.plugin.calls[0][1], self.event)
        self.assertNotIn(SECRET, json.dumps(result))

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_read_rejects_invalid_identifier_before_backend(self):
        for value in ("../private", "https://private.invalid/image", "a" * 63, "A" * 64, None):
            response = await self.tools["read_current_chat_image"].handler(self.event, value)
            self.assertTrue(response.model_dump(by_alias=True)["isError"])
        self.assertEqual(self.plugin.calls, [])

    @unittest.skipUnless(importlib.util.find_spec("mcp"), "MCP runtime not installed")
    async def test_read_refuses_oversized_invalid_and_nonimage_results(self):
        for result in ({"ok": True, "mime": "text/html", "data": PNG},
                       {"ok": True, "mime": "image/png", "data": "not-base64"},
                       {"ok": True, "mime": "image/png", "data": base64.b64encode(b"secret text").decode()},
                       {"ok": True, "mime": "image/png", "data": base64.b64encode(b"x" * (history.MAX_IMAGE_BYTES + 1)).decode()},
                       {"ok": False, "error": SECRET}):
            self.plugin.read_result = result
            response = await self.tools["read_current_chat_image"].handler(self.event, IMAGE_ID)
            self.assertTrue(response.model_dump(by_alias=True)["isError"])
            self.assertEqual(len(response.content), 1)
            self.assertNotIn(SECRET, response.content[0].text)


if __name__ == "__main__":
    unittest.main()

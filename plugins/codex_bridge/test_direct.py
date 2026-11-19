import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

from test_plugin import Event, Image, JID, UMO, job, plugin


class DirectRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.context = types.SimpleNamespace(send_message=AsyncMock(), get_all_stars=lambda: [])
        self.bridge = plugin.Main(self.context, {"allowed_umos": [UMO], "codex_first_enabled": True})
        self.bridge.routes_file = Path(self.temp.name) / "jobs.json"

    async def test_any_addressed_natural_language_goes_directly_once_without_business_gates(self):
        self.bridge._submit = AsyncMock()
        for text in ("你好", "记住我喜欢简洁的回答", "明天下午三点提醒我买菜", "帮我查去杭州的车",
                     "向我老婆讲解一下这个", "https://www.xiaohongshu.com/explore/synthetic-note", "接着办"):
            with self.subTest(text=text):
                event = Event(text)
                event.set_extra("xhs_notes_request", True)
                event.set_extra("travel_local_request", True)
                event.set_extra("intent_router_route", "xhs_search")
                self.bridge._submit.reset_mock()
                await self.bridge.route_directly_to_codex(event)
                await self.bridge.route_directly_to_codex(event)
                self.bridge._submit.assert_awaited_once_with(event, text, "read-only")
                self.assertTrue(event.stopped)
                self.assertFalse(event.llm)
                self.assertEqual(event.sent, [])

    async def test_pure_mode_is_submitted_once_even_when_direct_and_natural_routes_both_run(self):
        self.bridge._rpc = AsyncMock(return_value={"ok": True, "job": job()})
        event = Event("全用 Codex 设置一个两分钟后的提醒")
        await self.bridge.route_directly_to_codex(event)
        await self.bridge.natural_status(event)
        self.bridge._rpc.assert_awaited_once()
        payload = self.bridge._rpc.call_args.args[0]
        self.assertEqual(payload["execution_profile"], "pure-codex")
        self.assertIn("纯血 Codex 执行模式", payload["prompt"])

    async def test_default_off_invalid_config_and_unauthorized_messages_fall_through(self):
        self.bridge._submit = AsyncMock()
        for value in (None, False, "false", 1):
            self.bridge.config["codex_first_enabled"] = value
            event = Event("你好")
            await self.bridge.route_directly_to_codex(event)
            self.assertFalse(event.stopped)
        self.bridge.config["codex_first_enabled"] = True
        self.bridge.config["allowed_senders"] = ["alice"]
        unaddressed = Event("你好")
        unaddressed.is_at_or_wake_command = False
        other_platform = Event("你好")
        other_platform.get_platform_name = lambda: "other"
        for event in (Event("你好", umo="other"), Event("你好", sender="bob"), Event("你好", sender=""),
                      unaddressed, other_platform):
            await self.bridge.route_directly_to_codex(event)
            self.assertFalse(event.stopped)
            self.assertEqual(event.sent, [])
        self.bridge._submit.assert_not_awaited()

    async def test_explicit_commands_and_codex_opt_out_keep_existing_routes(self):
        self.bridge._submit = AsyncMock()
        for text in ("/codex status", "/xhs login", "/提醒列表", "/travel 杭州", "不要用 Codex，直接回答"):
            event = Event(text)
            await self.bridge.route_directly_to_codex(event)
            self.assertFalse(event.stopped)
        self.bridge._submit.assert_not_awaited()

    async def test_status_query_uses_local_board_without_creating_a_job(self):
        self.bridge._submit = AsyncMock()
        self.bridge._show = AsyncMock()
        event = Event("看下现在状态", quoted=f"【Codex 任务 #{JID}】")
        await self.bridge.route_directly_to_codex(event)
        self.bridge._show.assert_awaited_once_with(event, "status", JID)
        self.bridge._submit.assert_not_awaited()
        self.assertTrue(event.stopped)

    async def test_image_only_message_is_not_silently_dropped(self):
        self.bridge._submit = AsyncMock()
        event = Event()
        event.parts.append(Image(file="synthetic.png"))
        await self.bridge.route_directly_to_codex(event)
        self.assertIn("需求不明确时先询问", self.bridge._submit.call_args.args[1])
        self.assertEqual(self.bridge._submit.call_args.args[2], "read-only")

    async def test_real_submission_has_codex_instructions_and_durable_direct_marker(self):
        self.bridge._rpc = AsyncMock(return_value={"ok": True, "job": job()})
        event = Event("向我老婆讲解一下这个")
        await self.bridge.route_directly_to_codex(event)
        payload = self.bridge._rpc.call_args.args[0]
        self.assertIn("[Codex工具执行策略]", payload["prompt"])
        self.assertIn("脱敏原始详情", payload["prompt"])
        self.assertIn("原请求接收时间：", payload["prompt"])
        self.assertEqual(payload["mode"], "read-only")
        route = next(iter(self.bridge.routes.values()))
        self.assertNotIn("recipient", route)
        snapshot = json.loads(self.bridge._request_context_path(route["id"]).read_text())
        self.assertTrue(snapshot["extras"]["codex_first_submitted"])
        self.assertFalse(any("原生 @ 选中" in text for text in event.sent))

    async def test_transcribed_voice_is_submitted_as_text_not_rejected_as_record(self):
        self.bridge._rpc = AsyncMock(return_value={"ok": True, "job": job()})
        event = Event()
        event.parts = [type("Record", (), {})()]
        event.set_extra("voice_input_transcribed", True)
        event.set_extra("voice_input_transcript", "明天提醒我带伞")
        await self.bridge.route_directly_to_codex(event)
        self.bridge._rpc.assert_awaited_once()
        self.assertIn("明天提醒我带伞", self.bridge._rpc.call_args.args[0]["prompt"])
        self.assertFalse(any("暂不支持" in text for text in event.sent))

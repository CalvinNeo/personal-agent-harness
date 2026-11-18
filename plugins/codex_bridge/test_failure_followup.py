import importlib
import types
import unittest
from unittest.mock import AsyncMock

from test_plugin import Event, plugin

core = importlib.import_module(plugin.__package__ + ".core")


class FailureFollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.p = plugin.Main(self.context, {"allowed_umos": ["qq:GroupMessage:allowed"], "codex_first_enabled": True})
        self.p._show = AsyncMock()
        self.p._submit = AsyncMock()

    def test_natural_query_extracts_failure_question_from_quoted_board(self):
        quote = "【Codex 任务 #aaaaaaaaaaaaaaaa】\n状态：失败 · 阶段：失败\n任务执行失败。"
        self.assertEqual(core.natural_query("看下为什么失败", quote), ("status", "aaaaaaaaaaaaaaaa"))

    def test_failure_text_exposes_storage_error_and_recovery(self):
        text = self.p._failure_text({
            "id": "aaaaaaaaaaaaaaaa", "state": "failed", "phase": "failed",
            "created_at": "2026-01-01T10:00:00+00:00", "finished_at": "2026-01-01T10:10:00+00:00",
            "error": "Conversation storage limit exceeded", "delivery": "delivered",
        })
        self.assertIn("Conversation storage limit exceeded", text)
        self.assertIn("磁盘空间超过配额", text)
        self.assertIn("临时文件", text)

    async def test_quoted_failure_question_shows_status_without_submitting(self):
        event = Event("看下为什么失败", quoted=(
            "【Codex 任务 #aaaaaaaaaaaaaaaa】\n状态：失败 · 阶段：失败\n任务执行失败。"))
        await self.p.route_directly_to_codex(event)
        self.p._show.assert_awaited_once_with(event, "status", "aaaaaaaaaaaaaaaa")
        self.p._submit.assert_not_awaited()

    async def test_failed_retry_creates_fresh_bounded_task(self):
        event = Event("重试，可以将它拆成多个独立任务跑", quoted=(
            "【Codex 任务 #aaaaaaaaaaaaaaaa】\n状态：失败 · 阶段：失败\n任务执行失败。"))
        await self.p.route_directly_to_codex(event)
        self.p._submit.assert_awaited_once()
        prompt = self.p._submit.call_args.args[1]
        self.assertIn("新建一个新的 Codex 任务", prompt)
        self.assertIn("最多 2 条", prompt)
        self.p._show.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

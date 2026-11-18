import types
import unittest
from unittest.mock import AsyncMock

from test_plugin import Event, plugin


class SubtaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.p = plugin.Main(self.context, {"allowed_umos": ["qq:GroupMessage:allowed"], "codex_first_enabled": True})

    async def test_broker_creates_unique_read_only_children(self):
        event = Event("拆分当前任务")
        event.set_extra("codex_first_submitted", True)
        self.p._submit = AsyncMock(side_effect=lambda *args, **kwargs: {
            "ok": True, "job": {"id": kwargs["job_id_override"]}, "status_board": "accepted"})
        result = await self.p._create_codex_subtasks(event, [{"title": "A", "prompt": "做A"}, {"title": "B", "prompt": "做B"}])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["created"]), 2)
        calls = self.p._submit.await_args_list
        self.assertEqual(len({call.kwargs["job_id_override"] for call in calls}), 2)
        self.assertTrue(all(call.args[2] == "read-only" for call in calls))
        self.assertTrue(all("新建一个独立" in call.args[1] for call in calls))

    async def test_child_depth_is_denied(self):
        event = Event("child")
        event.set_extra("codex_subtask_depth", 1)
        result = await self.p._create_codex_subtasks(event, [{"prompt": "nested"}])
        self.assertEqual(result["code"], "subtasks_not_authorized")


if __name__ == "__main__":
    unittest.main()

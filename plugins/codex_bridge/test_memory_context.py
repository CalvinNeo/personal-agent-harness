"""Exercise real bridge submission with fake scoped memory and runner RPC."""
import types
import unittest
from unittest.mock import Mock

import test_plugin as fixture


class MemoryContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture.Tests.setUp(self)

    def tearDown(self):
        fixture.Tests.tearDown(self)

    def reader(self, value):
        reader = Mock(return_value=value)
        instance = types.SimpleNamespace(memory_context_for_task=reader)
        self.context.get_all_stars = lambda: [types.SimpleNamespace(star_cls=instance)]
        return reader

    async def test_scoped_context_reaches_task_not_routing_record(self):
        memory = '\n[长期记忆数据] [{"content":"回答偏好简洁"}]'
        reader = self.reader(memory)
        event = fixture.Event("请用 Codex 调研 Python")
        await self.p._submit(event, "调研 Python", notify=False)
        reader.assert_called_once_with(event, "调研 Python")
        self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], "调研 Python" + memory)
        self.assertNotIn("回答偏好", self.p.routes_file.read_text())

    async def test_unauthorized_sender_never_queries_memory(self):
        reader = self.reader("private background")
        self.p.config["allowed_senders"] = ["alice"]
        result = await self.p._submit(fixture.Event(sender="other"), "task", notify=False)
        self.assertFalse(result["ok"])
        reader.assert_not_called()
        self.p._rpc.assert_not_awaited()

    async def test_full_task_preserved_without_partial_memory(self):
        self.reader("memory" * 100)
        prompt = "task" * 2950
        await self.p._submit(fixture.Event(), prompt, notify=False)
        self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], prompt)

    async def test_absent_plugin_or_failure_keeps_valid_submission(self):
        await self.p._submit(fixture.Event(message_id="first"), "task", notify=False)
        self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], "task")
        reader = self.reader("")
        reader.side_effect = RuntimeError("never print private content")
        await self.p._submit(fixture.Event(message_id="second"), "next task", notify=False)
        self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], "next task")

    async def test_empty_or_oversize_memory_not_injected(self):
        for index, value in enumerate(("", "m" * 6001)):
            self.reader(value)
            await self.p._submit(fixture.Event(message_id=str(index)), "task", notify=False)
            self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], "task")


if __name__ == "__main__":
    unittest.main()

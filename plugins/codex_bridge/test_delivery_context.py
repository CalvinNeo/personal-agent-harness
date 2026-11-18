"""Generated audiences and durable tool calls use the original QQ context."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_plugin import At, Event, JID, Plain, Reply, UMO, job, plugin


class Result:
    def __init__(self, text): self.chain = [Plain(text)]
    def is_llm_result(self): return True
    def use_markdown(self, value): self.use_markdown_ = value


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        reader = lambda event: ["<@alice> 是 <@wife> 的老公"]
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True), get_all_stars=lambda: [
            types.SimpleNamespace(name="long_term_memory", activated=True,
                                  star_cls=types.SimpleNamespace(saved_relationship_facts=reader))])
        self.bridge = plugin.Main(self.context, {"allowed_umos": [UMO]})
        self.bridge.routes_file = Path(self.temp.name) / "jobs.json"
        self.bridge._rpc = AsyncMock(return_value={"ok": True, "job": job(conversation_id=JID)})

    def tearDown(self): self.temp.cleanup()

    async def test_exact_generated_reply_does_not_echo_topic_and_decorates_native_at(self):
        event = Event("向我老婆讲解一下汗液疗法")
        await self.bridge.capture_reply_recipient(event)
        await self.bridge.mention_current_sender(event)
        self.assertFalse(event.stopped)
        self.assertEqual(event.sent, [])
        result = Result("这是生成的讲解。")
        event.get_result = lambda: result
        await self.bridge.decorate_reply_recipient(event)
        self.assertEqual(result.chain[0].qq, "wife")
        self.assertEqual(result.chain[1].text, "这是生成的讲解。")
        self.assertTrue(result.use_markdown_)

    async def test_normal_receipt_survives_restart_and_only_relevant_quote_inherits(self):
        event = Event("给我老婆介绍下汗液疗法")
        event.get_result = lambda: Result("生成的解释")
        await self.bridge.decorate_reply_recipient(event)
        event.set_extra("qqofficial_sent_message_ids", ["actual-qq-response"])
        await self.bridge.remember_semantic_reply_receipts(event)
        restarted = plugin.Main(self.context, {"allowed_umos": [UMO]})
        restarted.routes_file = self.bridge.routes_file
        restarted._load()
        for text, expected in (("再详细一点", "wife"), ("明天天气如何", None), ("不要@她，继续讲", None)):
            followup = Event(text)
            followup.parts.append(Reply(id="actual-qq-response"))
            metadata = restarted._recipient_context(followup).metadata()
            self.assertEqual(metadata["target_id"] if metadata else None, expected)
        other = Event("再详细一点", sender="bob")
        other.parts.append(Reply(id="actual-qq-response"))
        self.assertIsNone(restarted._recipient_context(other).metadata())

    async def test_codex_submit_retains_recipient_final_recovery_and_no_progress_at(self):
        event = Event("向我老婆讲解一下汗液疗法")
        submitted = await self.bridge._submit(event, event.parts[0].text)
        self.assertTrue(submitted["ok"])
        route = next(iter(self.bridge.routes.values()))
        self.assertNotIn('recipient', route)
        registry = self.bridge._registries[route['id']]
        await self.bridge._execute_tool_call(route, registry, 'd' * 32, 'set_current_reply_recipient', {'target_label': '我老婆'})
        self.assertTrue(route['tool_receipts']['d' * 32]['result']['ok'])
        self.assertEqual(route["recipient"]["target_id"], "wife")
        self.assertFalse(any(isinstance(part, At) for part in event.sent_chains[0].chain))
        self.bridge._events.clear()
        self.bridge._load()
        route = self.bridge.routes[route["id"]]
        self.assertEqual(route["recipient"]["target_id"], "wife")
        self.assertTrue(await self.bridge._deliver_final(route, job(state="completed")))
        chain = self.context.send_message.call_args.args[1]
        self.assertEqual(chain.chain[0].qq, "wife")
        self.assertTrue(chain.use_markdown_)

    async def test_tool_receipt_replays_lost_rpc_response_without_duplicate_mutation(self):
        cid = "a" * 32
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        registry = types.SimpleNamespace(execute_codex=AsyncMock(return_value={"ok": True, "id": "actual-id"}),
            drain_deliveries=lambda _: [], recipient=None)
        self.bridge._registries[JID] = registry
        calls = [{"call_id": cid, "name": "create_current_reminder", "arguments": {}}]
        async def rpc(payload): return {"ok": True, "calls": calls} if payload["op"] == "tool_pending" else {"ok": True}
        self.bridge._rpc.side_effect = rpc
        await self.bridge._service_tool_calls(route)
        await self.bridge._service_tool_calls(route)
        calls[0]["call_id"] = "b" * 32
        await self.bridge._service_tool_calls(route)
        registry.execute_codex.assert_awaited_once()
        self.assertEqual(route["tool_receipts"][cid]["result"]["id"], "actual-id")
        restored = plugin.Main(self.context, {"allowed_umos": [UMO]})
        restored.routes_file = self.bridge.routes_file
        restored._load()
        self.assertEqual(restored.routes[JID]["tool_receipts"][cid]["state"], "complete")

    async def test_started_mutation_after_restart_is_not_executed_twice(self):
        cid = "c" * 32
        import hashlib
        fingerprint = hashlib.sha256(json.dumps(["create_current_reminder", {}], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice",
                 "tool_receipts": {cid: {"state": "started", "fingerprint": fingerprint}}}
        self.bridge.routes[JID] = route
        registry = types.SimpleNamespace(execute_codex=AsyncMock())
        self.bridge._registries[JID] = registry
        self.bridge._rpc.side_effect = lambda payload: {"ok": True, "calls": [{"call_id": cid, "name": "create_current_reminder", "arguments": {}}]} if payload["op"] == "tool_pending" else {"ok": True}
        await self.bridge._service_tool_calls(route)
        registry.execute_codex.assert_not_awaited()
        self.assertEqual(self.bridge._rpc.call_args.args[0]["result"]["code"], "execution_uncertain")

    async def test_large_multimodal_receipt_is_private_and_replayable(self):
        cid = "d" * 32
        route = {"id": JID, "tool_receipts": {cid: {"state": "started"}}}
        self.bridge.routes[JID] = route
        result = {"_mcp_result": {"content": [{"type": "image", "mimeType": "image/png", "data": "a" * 300000}]}}
        self.bridge._store_tool_result(route, cid, result)
        self.bridge._save()
        self.assertLess(self.bridge.routes_file.stat().st_size, 1000)
        self.assertEqual(self.bridge._stored_tool_result(route, route["tool_receipts"][cid]), result)
        payload = next(self.bridge.routes_file.parent.glob("request-contexts/*.result.json"))
        self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
        payload.write_text("{}")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.bridge._stored_tool_result(route, route["tool_receipts"][cid])
        self.bridge._cleanup_request_context(route)
        self.assertFalse(payload.exists())


if __name__ == "__main__": unittest.main()

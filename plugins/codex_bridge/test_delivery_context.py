"""Generated audiences and durable tool calls use the original QQ context."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from test_plugin import At, Chain, Event, JID, Plain, Reply, UMO, job, plugin


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

    async def test_native_baseline_pages_resume_after_known_rejection_and_restart(self):
        cid = "e" * 32
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        pages = [Chain([Plain(f"{index + 1}. [Synthetic item](https://www.goofish.com/item?id={100 + index})")], use_markdown_=True) for index in range(4)]
        registry = types.SimpleNamespace(execute_codex=AsyncMock(return_value={"ok": True, "delivery_pending": True,
            "delivery_plan": {"total_items": 4, "chunk_count": 4}}), drain_deliveries=lambda _: pages,
            trusted_text_delivery=lambda _, chain: chain.use_markdown_ is True, recipient=None)
        self.context.send_message.side_effect = [True, False]
        await self.bridge._execute_tool_call(route, registry, cid, "show_xianyu_baseline", {})
        receipt = route["tool_receipts"][cid]
        self.assertEqual(receipt["result"]["code"], "text_delivery_failed")
        self.assertEqual(receipt["text_plan"]["next"], 1)
        restarted = plugin.Main(self.context, {"allowed_umos": [UMO]})
        restarted.routes_file = self.bridge.routes_file
        restarted._load()
        restarted._rpc = AsyncMock(return_value={"ok": True})
        self.context.send_message.reset_mock(side_effect=True)
        self.context.send_message.return_value = True
        await restarted._execute_tool_call(restarted.routes[JID], registry, cid, "show_xianyu_baseline", {})
        result = restarted.routes[JID]["tool_receipts"][cid]["result"]
        self.assertTrue(result["complete"] and result["text_delivered"])
        self.assertEqual(result["delivered_count"], 4)
        self.assertEqual(self.context.send_message.await_count, 3)
        self.assertTrue(all(call.args[1].use_markdown_ for call in self.context.send_message.call_args_list))
        self.assertTrue(self.context.send_message.call_args_list[0].args[1].chain[0].text.startswith("2."))
        registry.execute_codex.assert_awaited_once()
        self.context.send_message.reset_mock()
        await restarted._execute_tool_call(restarted.routes[JID], registry, cid, "show_xianyu_baseline", {})
        self.context.send_message.assert_not_awaited()

    async def test_native_page_crash_is_uncertain_and_does_not_duplicate(self):
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        receipt = {"text_plan": {"pages": ["A", "B"], "next": 1, "sending": 1, "result": {"ok": True}}}
        result = await self.bridge._deliver_tool_text_plan(route, receipt)
        self.assertEqual(result["code"], "delivery_uncertain")
        self.assertFalse(result["complete"])
        self.context.send_message.assert_not_awaited()

    async def test_failed_execution_retry_inherits_page_progress_without_rerunning_display(self):
        import hashlib
        cid = "d" * 32
        fingerprint = hashlib.sha256(json.dumps(["show_xianyu_baseline", {}], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        original = {"id": "a" * 16, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice",
            "tool_receipts": {"a" * 32: {"name": "show_xianyu_baseline", "fingerprint": fingerprint, "state": "complete",
                "text_plan": {"pages": ["first page", "second page"], "next": 1, "sending": None,
                    "result": {"ok": True, "delivery_plan": {"total_items": 20, "chunk_count": 2}}}}}}
        route = {"id": JID, "owner": original["owner"], "umo": UMO, "sender": "alice", "auto_retry_of": original["id"]}
        self.bridge.routes.update({original["id"]: original, JID: route})
        registry = types.SimpleNamespace(execute_codex=AsyncMock(), recipient=None)
        await self.bridge._execute_tool_call(route, registry, cid, "show_xianyu_baseline", {})
        registry.execute_codex.assert_not_awaited()
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.context.send_message.call_args.args[1].chain[0].text, "second page")
        self.assertTrue(route["tool_receipts"][cid]["result"]["complete"])

    async def test_missing_display_page_is_rejected_before_any_page_is_sent(self):
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        registry = types.SimpleNamespace(execute_codex=AsyncMock(return_value={"ok": True,
            "delivery_plan": {"chunk_count": 2, "total_items": 20}}), recipient=None,
            drain_deliveries=lambda _: [Chain([Plain("one page")], use_markdown_=True)],
            trusted_text_delivery=lambda *_: True)
        await self.bridge._execute_tool_call(route, registry, "b" * 32, "show_xianyu_baseline", {})
        self.context.send_message.assert_not_awaited()
        self.assertEqual(route["tool_receipts"]["b" * 32]["result"]["code"], "display_missing")

    async def test_trusted_normal_handoff_sends_only_remaining_pages_and_counts_all(self):
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        receipt = {"ok": True, "delivery_plan": {"chunk_count": 4, "total_items": 40}}
        registry = types.SimpleNamespace(execute_codex=AsyncMock(return_value=receipt), recipient=None,
            drain_deliveries=lambda _: [Chain([Plain("remaining page " + str(i))], use_markdown_=True) for i in range(3)],
            trusted_text_delivery=lambda *_: True,
            trusted_text_progress=lambda *_: {"delivered_chunks": 1, "remaining_chunks": 3, "complete": False})
        await self.bridge._execute_tool_call(route, registry, "c" * 32, "show_xianyu_baseline", {})
        self.assertEqual(self.context.send_message.await_count, 3)
        result = route["tool_receipts"]["c" * 32]["result"]
        self.assertTrue(result["complete"])
        self.assertEqual((result["delivered_chunks"], result["delivered_count"]), (4, 40))

    async def test_empty_actual_qq_receipt_keeps_page_uncertain_and_replay_checks_current_authorization(self):
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        receipt = {"name": "show_xianyu_baseline", "text_plan": {"pages": ["page"], "next": 0, "sending": None,
            "result": {"ok": True, "delivery_plan": {"chunk_count": 1, "total_items": 1}}}}
        async def no_qq_receipt(scope, chain):
            chain.qqofficial_sent_message_ids = []
        self.context.send_message.side_effect = no_qq_receipt
        denied = types.SimpleNamespace(text_delivery_allowed=lambda *_: False)
        result = await self.bridge._deliver_tool_text_plan(route, receipt, denied)
        self.assertEqual(result["code"], "not_authorized")
        self.context.send_message.assert_not_awaited()
        result = await self.bridge._deliver_tool_text_plan(route, receipt)
        self.assertEqual(result["code"], "delivery_uncertain")
        self.assertEqual(receipt["text_plan"]["next"], 0)
        self.assertEqual(receipt["text_plan"]["sending"], 0)

    async def test_arbitrary_tool_plain_output_cannot_bypass_model_reply(self):
        route = {"id": JID, "owner": plugin.owner_key(UMO, "alice"), "umo": UMO, "sender": "alice"}
        self.bridge.routes[JID] = route
        registry = types.SimpleNamespace(execute_codex=AsyncMock(return_value={"ok": True}),
            drain_deliveries=lambda _: [Chain([Plain("untrusted tool prose")], use_markdown_=True)], recipient=None)
        await self.bridge._execute_tool_call(route, registry, "f" * 32, "other_tool", {})
        self.context.send_message.assert_not_awaited()

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

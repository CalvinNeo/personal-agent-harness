"""Follow-up routing must retain owner-scoped Codex conversations and images."""
import importlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from test_plugin import Chain, Event, Image, JID, Plain, Reply, UMO, job, plugin


class FollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True), get_all_stars=Mock(return_value=[]))
        self.p = plugin.Main(self.context, {"allowed_umos": [UMO, "qq:GroupMessage:other"]})
        self.p.routes_file = self.root / "routes.json"
        self.owner = plugin.owner_key(UMO, "alice")
        self.parent = job("completed", owner=self.owner, conversation_id=JID, resumable=True, context_images=1)
        self.p._rpc = AsyncMock(side_effect=self.rpc)
        self.media = importlib.import_module(plugin.__package__ + ".media")

    def tearDown(self):
        self.temp.cleanup()

    def rpc(self, payload):
        if payload["op"] == "status":
            if payload["owner"] != self.owner:
                return {"ok": False}
            return {"ok": True, "job": dict(self.parent)}
        if payload["op"] == "submit":
            return {"ok": True, "job": job(id=payload["job_id"], conversation_id=JID if payload.get("parent_job_id") else payload["job_id"], parent_job_id=payload.get("parent_job_id"), resumable=True, context_images=1 if payload.get("parent_job_id") else 0)}
        return {"ok": True}

    def event(self, text="原图日期有的，你再看看", **kwargs):
        return Event(text, quoted=f"【Codex 任务 #{JID}】\n原回复报告", **kwargs)

    def route(self, ids=(), **kwargs):
        item = {"id": JID, "owner": self.owner, "umo": UMO, "sender": "alice", "created": 100, "last_progress": 100, "progress_count": 0, "attempts": 0, "next_retry": 0, "delivered": True, "acked": True, "reply_message_ids": list(ids), **kwargs}
        self.p.routes[JID] = item
        return item

    async def test_short_reply_routes_to_original_conversation_and_preserves_images(self):
        event = self.event()
        image_selector = Mock(side_effect=AssertionError("must not inspect unrelated cached images"))
        self.context.get_all_stars.return_value = [types.SimpleNamespace(star_cls=types.SimpleNamespace(select_for_task=image_selector))]
        await self.p.natural_status(event)
        calls = [call.args[0] for call in self.p._rpc.await_args_list]
        self.assertEqual([call["op"] for call in calls], ["status", "submit"])
        self.assertEqual(calls[0], {"op": "status", "owner": self.owner, "job_id": JID})
        self.assertEqual(calls[1]["parent_job_id"], JID)
        self.assertEqual(calls[1]["prompt"], "原图日期有的，你再看看")
        self.assertEqual(calls[1]["mode"], "read-only")
        self.assertNotIn("images", calls[1])
        self.assertTrue(event.stopped)
        self.assertFalse(event.llm)
        self.assertIn("同会话续接", event.sent[0])
        self.assertIn("会话原图：1 张", event.sent[0])
        image_selector.assert_not_called()
        saved = json.loads(self.p.routes_file.read_text())["jobs"][0]
        self.assertEqual(saved["parent_job_id"], JID)
        self.assertNotIn("原回复报告", self.p.routes_file.read_text())

    async def test_very_short_comments_do_not_require_action_regex(self):
        for index, text in enumerate(("不对", "继续", "日期呢？", "这个部分漏了", "补上右下角", "谢谢")):
            event = self.event(text, message_id=str(index))
            await self.p.natural_status(event)
            self.assertEqual(self.p._rpc.call_args.args[0]["prompt"], text)
            self.assertEqual(self.p._rpc.call_args.args[0]["parent_job_id"], JID)

    async def test_queued_or_running_parent_can_accept_followup_turn(self):
        for state in ("queued", "running"):
            self.parent.update(state=state)
            await self.p.natural_status(self.event(message_id=state))
            self.assertEqual(self.p._rpc.call_args.args[0]["op"], "submit")
            self.assertEqual(self.p._rpc.call_args.args[0]["parent_job_id"], JID)

    async def test_status_and_result_queries_still_use_existing_query_route(self):
        for text in ("看下现在状态", "结果呢"):
            event = self.event(text)
            self.p._rpc.reset_mock()
            await self.p.natural_status(event)
            self.p._rpc.assert_awaited_once_with({"op": "status", "owner": self.owner, "job_id": JID})
            self.assertTrue(event.stopped)

    async def test_reply_message_id_recovers_placeholder_only_result(self):
        self.route(ids=["qq-result-message"])
        event = Event("原图日期有的，你再看看", quoted="[图片]")
        event.parts[-1].id = "qq-result-message"
        await self.p.natural_status(event)
        payload = self.p._rpc.call_args.args[0]
        self.assertEqual(payload["parent_job_id"], JID)
        self.assertEqual(payload["prompt"], "原图日期有的，你再看看")
        self.assertIn("同会话续接", event.sent[0])

    async def test_message_id_status_query_is_not_submitted_as_followup(self):
        self.route(ids=["qq-image-message"])
        event = Event("看下状态", quoted="[图片]")
        event.parts[-1].id = "qq-image-message"
        await self.p.natural_status(event)
        self.p._rpc.assert_awaited_once_with({"op": "status", "owner": self.owner, "job_id": JID})

    async def test_message_mapping_cannot_resume_different_sender(self):
        self.route(ids=["qq-result-message"])
        event = Event("再看看", quoted="[图片]", sender="bob")
        event.parts[-1].id = "qq-result-message"
        await self.p.natural_status(event)
        self.assertFalse(event.stopped)
        self.p._rpc.assert_not_awaited()

    async def test_server_owner_check_precedes_any_image_read(self):
        for kwargs in ({"sender": "bob"}, {"umo": "qq:GroupMessage:other"}):
            event = self.event(**kwargs)
            event.parts.append(Image("https://gchat.qpic.cn/sensitive"))
            with patch.object(self.media, "prepare_images", new_callable=AsyncMock) as prepare:
                await self.p.natural_status(event)
            prepare.assert_not_awaited()
            self.assertIn("属于你在本群", event.sent[0])
        self.assertTrue(all(call.args[0]["op"] == "status" for call in self.p._rpc.await_args_list))
        self.assertFalse(self.p.routes_file.exists())

    async def test_denied_group_does_not_lookup_parent_or_prepare_images(self):
        event = self.event(umo="denied")
        with patch.object(self.media, "prepare_images", new_callable=AsyncMock) as prepare:
            await self.p.natural_status(event)
        self.assertTrue(event.stopped)
        self.p._rpc.assert_not_awaited()
        prepare.assert_not_awaited()
        self.assertEqual(event.sent, [])

    async def test_legacy_ephemeral_session_is_not_silently_replaced(self):
        self.parent["resumable"] = False
        event = self.event()
        with patch.object(self.media, "prepare_images", new_callable=AsyncMock) as prepare:
            await self.p.natural_status(event)
        prepare.assert_not_awaited()
        self.p._rpc.assert_awaited_once()
        self.assertIn("没有可恢复的会话", event.sent[0])
        self.assertIn("当前没有提交", event.sent[0])
        self.assertFalse(self.p.routes_file.exists())

    async def test_parent_timeout_is_explicit_and_does_not_create_new_job(self):
        self.p._rpc.side_effect = TimeoutError("secret")
        event = self.event()
        await self.p.natural_status(event)
        self.p._rpc.assert_awaited_once()
        self.assertIn("无法验证原 Codex 会话", event.sent[0])
        self.assertNotIn("secret", event.sent[0])
        self.assertFalse(self.p.routes_file.exists())

    async def test_plain_quotes_never_become_codex_tasks(self):
        for quote in ("普通机器人回答", "他说请用 Codex 调研", "任务编号 #12345678abcdef01", "【Codex 任务 #12345678】"):
            event = Event("原图日期有的，你再看看", quoted=quote)
            await self.p.natural_status(event)
            self.assertFalse(event.stopped)
        self.p._rpc.assert_not_awaited()

    async def test_optout_does_not_resume_and_explicit_new_task_starts_new_conversation(self):
        event = self.event("不要用 Codex，直接回答")
        await self.p.natural_status(event)
        self.assertFalse(event.stopped)
        self.p._rpc.assert_not_awaited()
        event = self.event("新建一个新的 Codex 任务：整理公开价格信息")
        await self.p.natural_status(event)
        self.p._rpc.assert_awaited_once()
        payload = self.p._rpc.call_args.args[0]
        self.assertEqual(payload["op"], "submit")
        self.assertNotIn("parent_job_id", payload)
        self.assertIn("新会话", event.sent[0])

    async def test_current_image_adds_to_resumed_context_and_replaces_no_old_cache(self):
        from PIL import Image as PillowImage
        path = self.root / "new.png"
        PillowImage.new("RGB", (60, 30), "orange").save(path)
        event = self.event("继续，补充这张图一起看")
        event.parts.append(Image(str(path)))
        with patch.object(self.media, "_temp_root", return_value=self.root):
            await self.p.natural_status(event)
        payload = self.p._rpc.call_args.args[0]
        self.assertEqual(payload["parent_job_id"], JID)
        self.assertEqual(len(payload["images"]), 1)
        self.assertEqual(payload["images"][0]["mime"], "image/jpeg")
        self.assertEqual(payload["prompt"], "继续，补充这张图一起看")

    async def test_direct_command_also_validates_and_resumes_parent(self):
        event = self.event("/codex 再看一下日期")
        await self.p.codex(event, "再看一下日期")
        payload = self.p._rpc.call_args.args[0]
        self.assertEqual(payload["parent_job_id"], JID)
        self.assertEqual(payload["prompt"], "再看一下日期")

    async def test_chain_receipts_override_cumulative_event_ids_and_persist(self):
        route = self.route()
        event = Event()
        event.set_extra("qqofficial_sent_message_ids", ["unrelated-before"])
        async def send(chain):
            chain.qqofficial_sent_message_ids = ["exact-reply"]
            event.set_extra("qqofficial_sent_message_ids", ["unrelated-before", "other-plugin", "exact-reply"])
        event.send = send
        await self.p._send(event, f"【Codex 任务 #{JID}】\n结果")
        self.assertEqual(route["reply_message_ids"], ["exact-reply"])
        recovered = plugin.Main(self.context, self.p.config)
        recovered.routes_file = self.p.routes_file
        recovered._load()
        self.assertEqual(recovered.routes[JID]["reply_message_ids"], ["exact-reply"])

    async def test_proactive_receipts_persist_without_event(self):
        route = self.route()
        async def send(umo, chain):
            chain.qqofficial_sent_message_ids = ["proactive-result"]
            return True
        self.context.send_message.side_effect = send
        self.assertTrue(await self.p._deliver(route, f"【Codex 任务 #{JID}】\n结果"))
        self.assertEqual(route["reply_message_ids"], ["proactive-result"])

    async def test_event_receipts_fallback_only_records_new_ids(self):
        route = self.route()
        event = Event()
        event.set_extra("qqofficial_sent_message_ids", ["earlier"])
        async def send(chain):
            event.set_extra("qqofficial_sent_message_ids", ["earlier", "new-only"])
        event.send = send
        await self.p._send(event, f"【Codex 任务 #{JID}】")
        self.assertEqual(route["reply_message_ids"], ["new-only"])

    async def test_embedded_new_task_phrase_does_not_reset_conversation(self):
        event = self.event("分析新建一个新的 Codex 任务的流程，补上原报告")
        await self.p.natural_status(event)
        self.assertEqual(self.p._rpc.call_args.args[0]["parent_job_id"], JID)

    async def test_resume_parent_cannot_be_supplied_without_current_reply(self):
        result = await self.p._submit(Event("继续"), "继续", parent_job_id=JID, notify=False)
        self.assertFalse(result["ok"])
        self.assertIn("必须引用", result["error"])
        self.p._rpc.assert_not_awaited()

    async def test_read_only_followup_does_not_inherit_parent_write_mode(self):
        self.parent["mode"] = "workspace-write"
        await self.p.natural_status(self.event())
        self.assertEqual(self.p._rpc.call_args.args[0]["mode"], "read-only")

    async def test_two_parent_replies_are_not_guessed(self):
        event = self.event()
        event.parts.append(Reply(message_str="【Codex 任务 #ffffffffffffffff】"))
        await self.p.natural_status(event)
        self.assertIn("多个 Codex 会话", event.sent[0])
        self.p._rpc.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

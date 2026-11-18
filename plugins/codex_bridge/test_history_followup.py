import types
import unittest
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock
import importlib

from test_plugin import Plain, Reply, Image, plugin
request_context = importlib.import_module(plugin.__package__ + ".request_context")
event_is_addressed = request_context.event_is_addressed

At = type("At", (), {})

class AddressedEventTests(unittest.TestCase):
    def event(self, parts, *, umo="qq:GroupMessage:allowed", wake=False, self_id="bot"):
        return types.SimpleNamespace(
            is_at_or_wake_command=wake, unified_msg_origin=umo,
            get_platform_name=lambda: "qq_official", get_self_id=lambda: self_id,
            get_messages=lambda: parts,
        )

    def test_group_top_level_bot_mention_is_addressed(self):
        mention = At(); mention.qq = "bot"
        self.assertTrue(event_is_addressed(self.event([Plain("task"), mention])))

    def test_nested_reply_mention_is_not_addressed(self):
        mention = At(); mention.qq = "bot"
        self.assertFalse(event_is_addressed(self.event([Reply("quoted", [mention])])))

    def test_private_message_is_addressed_without_wake_flag(self):
        self.assertTrue(event_is_addressed(self.event([Plain("task")], umo="qq:FriendMessage:alice")))

class DouyinHistoryFollowupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.p = plugin.Main(self.context, {"allowed_umos": ["qq:GroupMessage:allowed"], "codex_first_enabled": True})
        self.p.routes_file = Path(self.tmp.name) / "jobs.json"
        self.p._rpc = AsyncMock(return_value={"ok": True, "job": {"id": "12345678abcdef01", "state": "queued", "mode": "read-only", "phase": "queued", "created_at": "2026", "started_at": None, "finished_at": None, "last_event_at": "2026", "heartbeat_at": "2026", "result": None, "delivery": "pending"}})
        ChatHistory = importlib.import_module(plugin.__package__ + ".history").ChatHistory
        self.p._history_store = ChatHistory(Path(self.tmp.name) / "history.sqlite3")
        self.p._history_store.record("qq:GroupMessage:allowed", "received:old", "user", "https://v.douyin.com/old/", received_at=1)
        self.p._submit = AsyncMock(return_value={"ok": True})

    def tearDown(self):
        self.p._history_store.close()
        self.tmp.cleanup()

    async def test_quote_only_bot_mention_routes_from_history(self):
        event = types.SimpleNamespace()
        event.sender = "alice"; event.unified_msg_origin = "qq:GroupMessage:allowed"; event.is_at_or_wake_command = False
        event.message_obj = types.SimpleNamespace(message_id="mid", self_id="bot")
        mention = At(); mention.qq = "bot"
        event.parts = [Reply("刚才发了很多抖音视频，重新整理下"), mention]
        event.sent=[]; event.stopped=False; event.llm=True; event.extras={}
        event.get_platform_name=lambda: "qq_official"; event.get_sender_id=lambda: "alice"; event.get_messages=lambda: event.parts
        event.get_extra=lambda k: event.extras.get(k); event.set_extra=lambda k,v: event.extras.__setitem__(k,v)
        event.stop_event=lambda: setattr(event,"stopped",True); event.should_call_llm=lambda v: setattr(event,"llm",v)
        async def send(chain): event.sent.append("sent"); return True
        event.send=send
        await self.p.route_directly_to_codex(event)
        self.assertTrue(event.stopped)
        self.assertTrue(self.p._submit.await_count == 1)
        payload=self.p._submit.call_args.args
        self.assertEqual(payload[2], "read-only")
        self.assertIn("current_chat.search_current_chat_history", payload[1])

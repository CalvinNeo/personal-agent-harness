"""Current-group requests use native QQ transport, never the task worker."""
import hashlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_plugin import Event, Plain, Reply, UMO, plugin


class At:
    def __init__(self, qq):
        self.qq = qq


class Chain:
    def __init__(self, chain, use_markdown_=False):
        self.chain, self.use_markdown_ = chain, use_markdown_


class MentionPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bridge = plugin.Main(types.SimpleNamespace(send_message=AsyncMock()), {"allowed_umos": [UMO]})
        self.bridge.routes_file = Path(self.temp.name) / "jobs.json"
        self.bridge._rpc = AsyncMock()
        self.bridge._submit = AsyncMock()
        self.patches = [patch.object(plugin, "MessageChain", Chain),
                        patch.object(sys.modules["astrbot.api.message_components"], "At", At, create=True)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def event(self, text, *, targets=(), **kwargs):
        source = Event(text, **kwargs)
        source.message_obj.self_id = "bot"
        source.parts += [At("bot"), *(At(value) for value in targets)]
        source.send = AsyncMock(return_value=True)
        return source

    async def test_exact_failure_clarifies_before_timeout_or_quoted_codex_resume(self):
        source = self.event("at 我老婆，告诉她胖了", quoted="【Codex 任务 #12345678abcdef01】")
        source.set_extra("intent_router_timeout", {
            "event": id(source), "owner": plugin.owner_key(UMO, "alice"),
            "text": hashlib.sha256(source.parts[0].text.encode()).hexdigest(),
        })
        await self.bridge.natural_status(source)
        await self.bridge.natural_status(source)
        self.bridge._rpc.assert_not_awaited()
        self.bridge._submit.assert_not_awaited()
        source.send.assert_awaited_once()
        text = source.send.call_args.args[0].chain[0].text
        self.assertIn("QQ 原生 @", text)
        self.assertIn("仅凭称呼无法确定账号", text)
        self.assertNotIn("无法直接发送", text)
        self.assertTrue(source.stopped)
        self.assertFalse(source.llm)

    async def test_native_target_sends_requested_body_through_original_event(self):
        source = self.event("at 我老婆，告诉她胖了", targets=("member",))
        await self.bridge.mention_current_sender(source)
        chain = source.send.call_args.args[0]
        self.assertTrue(chain.use_markdown_)
        self.assertEqual(chain.chain[0].qq, "member")
        self.assertEqual(chain.chain[1].text, "胖了")
        self.bridge.context.send_message.assert_not_awaited()
        self.bridge._submit.assert_not_awaited()

    async def test_quoted_target_cannot_select_recipient(self):
        source = self.event("at 我老婆，告诉她准备好了")
        source.parts.append(Reply(chain=[At("member")]))
        await self.bridge.mention_current_sender(source)
        self.assertIn("仅凭称呼", source.send.call_args.args[0].chain[0].text)

    async def test_unaddressed_and_nonallowlisted_events_cannot_send(self):
        denied = self.event("at 我", umo="qq:GroupMessage:other")
        asleep = self.event("at 我")
        asleep.is_at_or_wake_command = False
        asleep.parts = [part for part in asleep.parts if not isinstance(part, At)]
        private = self.event("at 我", umo="qq:FriendMessage:allowed")
        for source in (denied, asleep, private):
            await self.bridge.mention_current_sender(source)
            source.send.assert_not_awaited()

    async def test_native_bot_at_is_addressed_even_when_adapter_wake_flag_is_false(self):
        source = self.event('at 我，告诉我准备好了')
        source.is_at_or_wake_command = False
        await self.bridge.mention_current_sender(source)
        source.send.assert_awaited_once()
        self.assertEqual(source.send.call_args.args[0].chain[0].qq, 'alice')
        self.bridge._submit.assert_not_awaited()

    async def test_transport_failure_does_not_fall_back_to_proactive_message(self):
        source = self.event("at 我")
        source.send.return_value = False
        with self.assertRaisesRegex(RuntimeError, "QQ rejected"):
            await self.bridge.mention_current_sender(source)
        self.bridge.context.send_message.assert_not_awaited()
        self.bridge._submit.assert_not_awaited()

    async def test_voice_transcript_keeps_same_local_path(self):
        source = self.event("")
        source.set_extra("voice_input_transcribed", True)
        source.set_extra("voice_input_transcript", "at 我，告诉我准备好了")
        await self.bridge.natural_status(source)
        self.assertEqual(source.send.call_args.args[0].chain[0].qq, "alice")
        self.assertEqual(source.send.call_args.args[0].chain[1].text, "准备好了")

    def saved_family(self):
        reader = lambda source: ["本群人物关系：<@alice> 是<@wife> 的老公，<@father> 是<@alice> 的爸爸"]
        self.bridge.context.get_all_stars = lambda: [types.SimpleNamespace(
            name="long_term_memory", activated=True, star_cls=types.SimpleNamespace(saved_relationship_facts=reader))]

    async def test_stored_relationship_resolves_exact_original_request(self):
        self.saved_family()
        source = self.event("at 我老婆，告诉她胖了")
        await self.bridge.natural_status(source)
        chain = source.send.call_args.args[0]
        self.assertEqual(chain.chain[0].qq, "wife")
        self.assertEqual(chain.chain[1].text, "胖了")
        self.bridge._submit.assert_not_awaited()

    async def test_real_retry_of_owner_bound_legacy_mention_stays_local(self):
        self.saved_family()
        jid = "12345678abcdef01"
        self.bridge.routes[jid] = {"id": jid, "owner": plugin.owner_key(UMO, "alice"),
            "sender": "alice", "umo": UMO, "local_mention": {"target_label": "我老婆", "body": "胖了"}}
        self.bridge._save()
        self.bridge._load()
        for phrase in ("再试试呢", "重新提交"):
            source = self.event(phrase, quoted="【Codex 任务 #" + jid + "】")
            await self.bridge.natural_status(source)
            self.assertEqual(source.send.call_args.args[0].chain[0].qq, "wife")
            self.assertEqual(source.send.call_args.args[0].chain[1].text, "胖了")
        self.bridge._rpc.assert_not_awaited()
        self.bridge._submit.assert_not_awaited()

    async def test_legacy_mention_retry_cannot_read_another_owners_metadata(self):
        self.saved_family()
        jid = "12345678abcdef01"
        self.bridge.routes[jid] = {"id": jid, "owner": plugin.owner_key(UMO, "bob"),
            "sender": "bob", "umo": UMO, "local_mention": {"target_label": "我老婆", "body": "胖了"}}
        source = self.event("再试试呢", quoted="【Codex 任务 #" + jid + "】")
        await self.bridge.mention_current_sender(source)
        source.send.assert_not_awaited()

    async def test_native_target_takes_precedence_over_saved_relationship(self):
        self.saved_family()
        source = self.event("at 我老婆，告诉她准备好了", targets=("explicit-member",))
        await self.bridge.mention_current_sender(source)
        self.assertEqual(source.send.call_args.args[0].chain[0].qq, "explicit-member")


if __name__ == "__main__":
    unittest.main()

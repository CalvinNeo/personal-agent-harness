"""Real AstrBot events, bridge RPC codec, media processing and capability identities.

Only the Unix transport, image HTTP transport and QQ send are substituted.
No real task is submitted and no QQ message is sent by this test suite.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))

from astrbot.api.message_components import Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from codex_bridge.main import Main
from codex_bridge.core import owner_key
from codex_bridge.capabilities import CapabilityRegistry
from codex_bridge import media
from PIL import Image as PILImage

SCOPE = 'private_qq_bot:GroupMessage:shared-group'
OTHER_SCOPE = 'private_qq_bot:GroupMessage:other-group'
PRIVATE = 'private_qq_bot:FriendMessage:bob'
CHARLIE_PRIVATE = 'private_qq_bot:FriendMessage:charlie'
DENIED_SCOPE = 'private_qq_bot:GroupMessage:unapproved-group'
PARENT = 'a' * 16
CONVERSATION = 'b' * 16
PUBLIC = '合成公开群结果：这是一份测试清单。'
CURRENT_IMAGE = 'https://gchat.qpic.cn/current.png'
CACHED_QR = 'https://gchat.qpic.cn/old-login-qr.png'
stream = io.BytesIO()
PILImage.new('RGB', (32, 32), 'blue').save(stream, format='PNG')
PNG = stream.getvalue()
TOOL_NAME = 'manage_long_term_memory'
TOOL_DESCRIPTION = 'test current user memory mutation'
TOOL_SCHEMA = {'type': 'object', 'properties': {'content': {'type': 'string'}}, 'required': ['content'], 'additionalProperties': False}


def event(text='现在请在闲鱼搜索合成积木套件', scope=SCOPE, sender='alice', *, quote=True, direct_image=False, quote_text=None):
    group = scope.split(':')[-1] if ':GroupMessage:' in scope else ''
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.message = []
    if quote:
        value = quote_text if quote_text is not None else '【Codex 任务 #' + PARENT + '】\n' + PUBLIC
        msg.message.append(Reply(id='public-result-receipt', message_str=value, chain=[Plain(value)]))
    msg.message.append(Plain(text))
    if direct_image:
        msg.message.append(Image.fromURL(CURRENT_IMAGE))
    msg.message_str = text
    msg.sender = MessageMember(sender)
    msg.group_id = group
    msg.self_id = 'bot'
    msg.message_id = 'synthetic-shared-' + str(time.time_ns())
    result = AstrMessageEvent(text, msg, PlatformMetadata('qq_official', 'private_qq_bot', 'private_qq_bot'), group or scope.split(':')[-1])
    result.is_at_or_wake_command = True
    result.sent = []
    async def send(chain):
        result.sent.append(chain)
        result.set_extra('qqofficial_sent_message_ids', [*result.get_extra('qqofficial_sent_message_ids', []), 'sent-' + str(len(result.sent)) + '-' + msg.message_id])
        return True
    result.send = send
    assert result.unified_msg_origin == scope
    return result


def plain_sent(ev):
    return '\n'.join(part.text for chain in ev.sent for part in chain.chain if isinstance(part, Plain))


class Cache:
    _archive = object()
    def __init__(self):
        self.calls = 0
    def prepare_for_task(self, ev):
        self.calls += 1
        return [part.url or part.file for part in ev.get_messages() if isinstance(part, Image)] or [CACHED_QR]


class CurrentMemory:
    def __init__(self):
        self.identities = []
    def _allowed(self, ev):
        return ev.unified_msg_origin in {SCOPE, OTHER_SCOPE, PRIVATE, CHARLIE_PRIVATE}
    def memory_snapshot_for_task(self, ev):
        self.identities.append(('read', ev.unified_msg_origin, str(ev.get_sender_id())))
        return {'status': 'ok', 'items': [{'id': 'memory-1', 'content': 'current actor memory'}]}
    async def manage_memory_tool(self, ev, content, *, _codex_call=None):
        self.identities.append(('write', ev.unified_msg_origin, str(ev.get_sender_id())))
        return {'ok': True, 'owner': owner_key(ev.unified_msg_origin, str(ev.get_sender_id())), 'content': content}


class Registry:
    instructions = ''
    def __init__(self, text):
        self.snapshot = {'text': text, 'received_at': time.time()}
    def schemas(self, codex=False):
        return [{'function': {'name': 'list_my_long_term_memories', 'description': 'test read', 'parameters': {'type': 'object', 'properties': {}}}}]


class SharedSessions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Cache()
        self.context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(name='image_context', activated=True, star_cls=self.cache)])
        self.bridge = Main(self.context, {'allowed_umos': [SCOPE, OTHER_SCOPE, PRIVATE, CHARLIE_PRIVATE], 'codex_first_enabled': True, 'write_senders': []})
        self.bridge.routes_file = Path(self.tmp.name) / 'jobs.json'
        self.calls = []
        self.jobs = {}
        self.add_task(PARENT, SCOPE, 'bob')
        self.bridge._capability_registry = lambda ev, task_text=None: asyncio.sleep(0, result=Registry(task_text or ev.message_str))
        self.bridge._recipient_context = lambda ev: SimpleNamespace(metadata=lambda: None, schemas=lambda: [])
        async def connection(*args, **kwargs):
            outer = self
            class Writer:
                def write(self, raw):
                    self.payload = json.loads(raw)
                    outer.calls.append(self.payload)
                async def drain(self):
                    pass
                def close(self):
                    pass
                async def wait_closed(self):
                    pass
            writer = Writer()
            class Reader:
                async def readline(self):
                    return (json.dumps(outer.runner(writer.payload)) + '\n').encode()
            return Reader(), writer
        self.transport = patch('asyncio.open_unix_connection', new=connection)
        self.transport.start()
        self.download = patch.object(media, '_download', new=AsyncMock(return_value=PNG))
        self.downloader = self.download.start()

    def add_task(self, jid, scope, sender, *, created=1, result='合成共享结果', artifacts=None, runner_owner=None):
        owner = owner_key(scope, sender)
        route = {'id': jid, 'owner': owner, 'sender': sender, 'umo': scope, 'created': created,
                 'reply_message_ids': [], 'acked': False, 'text_delivered': False}
        if runner_owner:
            route['runner_owner'] = runner_owner
        self.bridge.routes[jid] = route
        self.jobs[jid] = {'id': jid, 'owner': runner_owner or owner, 'state': 'completed', 'phase': 'completed',
                          'mode': 'read-only', 'conversation_id': CONVERSATION, 'resumable': True,
                          'context_images': 1, 'created_at': f'2000-01-01T00:00:{created:02d}+00:00',
                          'result': result, 'artifacts': artifacts or []}
        return route

    def runner(self, payload):
        op = payload['op']
        if op == 'submit':
            parent = self.jobs.get(payload.get('parent_job_id'))
            if parent and payload['owner'] != parent['owner']:
                return {'ok': False, 'error': 'job not found'}
            job = {'id': payload['job_id'], 'owner': payload['owner'], 'state': 'queued', 'phase': 'queued',
                   'mode': payload['mode'], 'conversation_id': parent['conversation_id'] if parent else payload['job_id'],
                   'resumable': True, 'context_images': (parent['context_images'] if parent else 0) + len(payload.get('images', [])),
                   'created_at': '2000-01-01T01:00:00+00:00', 'artifacts': []}
            self.jobs[job['id']] = job
            return {'ok': True, 'job': job}
        if op == 'list':
            jobs = sorted((job for job in self.jobs.values() if job['owner'] == payload['owner']), key=lambda job: job['created_at'], reverse=True)
            return {'ok': True, 'jobs': jobs[:payload['limit']]}
        job = self.jobs.get(payload.get('job_id'))
        if not job or payload['owner'] != job['owner']:
            return {'ok': False, 'error': 'job not found'}
        if op == 'status':
            return {'ok': True, 'job': job}
        if op == 'artifact':
            return {'ok': True, 'artifact': {'mime': 'image/png', 'data': base64.b64encode(PNG).decode()}}
        if op in {'tool_pending', 'tool_result', 'delivery'}:
            return {'ok': True, 'calls': []}
        raise AssertionError(payload)

    async def asyncTearDown(self):
        self.download.stop()
        self.transport.stop()
        await self.bridge.terminate()
        self.tmp.cleanup()

    async def submit(self, ev, **kwargs):
        response = await self.bridge._submit(ev, ev.message_str, notify=False, **kwargs)
        self.assertTrue(response['ok'], response)
        return response

    async def test_another_group_member_resumes_real_session_and_retained_images(self):
        ev = event()
        response = await self.submit(ev)
        self.assertEqual([call['op'] for call in self.calls], ['status', 'submit'])
        wire = self.calls[-1]
        route = self.bridge.routes[response['job']['id']]
        self.assertEqual(wire['parent_job_id'], PARENT)
        self.assertEqual(wire['owner'], owner_key(SCOPE, 'bob'))
        self.assertEqual(route['owner'], owner_key(SCOPE, 'alice'))
        self.assertEqual(route['runner_owner'], owner_key(SCOPE, 'bob'))
        self.assertEqual(response['job']['context_images'], 1)
        self.assertNotIn(PUBLIC, wire['prompt'])
        self.assertFalse(wire.get('images'))
        self.assertEqual(self.cache.calls, 0)
        self.downloader.assert_not_awaited()

    async def test_own_reply_keeps_real_resume(self):
        self.bridge.routes[PARENT]['owner'] = self.jobs[PARENT]['owner'] = owner_key(SCOPE, 'alice')
        self.bridge.routes[PARENT]['sender'] = 'alice'
        await self.submit(event())
        self.assertEqual(self.calls[-1]['parent_job_id'], PARENT)

    async def test_allowed_private_source_to_group_keeps_current_actor_capability_identity(self):
        self.add_task(PARENT, PRIVATE, 'bob')
        memory = CurrentMemory()
        self.context.get_all_stars = lambda: [SimpleNamespace(name='long_term_memory', activated=True, star_cls=memory)]
        del self.bridge._capability_registry
        ev = event('继续分析并帮我记住我喜欢便宜的玩具')
        response = await self.submit(ev)
        route = self.bridge.routes[response['job']['id']]
        self.assertEqual(self.calls[-1]['owner'], owner_key(PRIVATE, 'bob'))
        self.assertEqual(self.calls[-1]['workspace'], hashlib.sha256(PRIVATE.encode()).hexdigest()[:32])
        self.assertEqual(route['owner'], owner_key(SCOPE, 'alice'))
        self.assertEqual(route['umo'], SCOPE)
        registry = self.bridge._registries[route['id']]
        self.assertIsInstance(registry, CapabilityRegistry)
        self.assertEqual(registry.snapshot['owner'], owner_key(SCOPE, 'alice'))
        self.assertEqual(registry.snapshot['sender'], 'alice')
        self.assertEqual(self.bridge._read_request_context(route)['owner'], owner_key(SCOPE, 'alice'))
        await self.bridge._execute_tool_call(route, registry, 'c' * 32, 'list_my_long_term_memories', {})
        read = route['tool_receipts']['c' * 32]['result']
        self.assertTrue(read['ok'], read)
        await self.bridge._execute_tool_call(route, registry, 'd' * 32, TOOL_NAME, {'content': '我喜欢便宜的玩具'})
        write = route['tool_receipts']['d' * 32]['result']
        self.assertTrue(write['ok'], write)
        self.assertEqual(write['owner'], owner_key(SCOPE, 'alice'))
        self.assertEqual(self.calls[-1]['op'], 'tool_result')
        self.assertEqual(self.calls[-1]['owner'], owner_key(PRIVATE, 'bob'))
        self.assertEqual(memory.identities, [('read', SCOPE, 'alice'), ('write', SCOPE, 'alice')])
        self.assertEqual(self.bridge.routes[PARENT]['umo'], PRIVATE)

    async def test_cross_scope_quote_from_group_to_private_resumes(self):
        response = await self.submit(event(scope=PRIVATE, sender='bob'))
        self.assertEqual(self.calls[-1]['owner'], owner_key(SCOPE, 'bob'))
        self.assertEqual(self.bridge.routes[response['job']['id']]['umo'], PRIVATE)

    async def test_source_unapproved_scope_cannot_be_resumed(self):
        self.add_task(PARENT, DENIED_SCOPE, 'bob')
        response = await self.bridge._submit(event(), '继续', notify=False)
        self.assertFalse(response['ok'])
        self.assertEqual(self.calls, [])

    async def test_current_unapproved_scope_cannot_access_shared_task(self):
        ev = event(scope=DENIED_SCOPE)
        response = await self.bridge._submit(ev, '继续', notify=False)
        self.assertFalse(response['ok'])
        await self.bridge._show(ev, 'result', PARENT)
        self.assertEqual(self.calls, [])
        self.assertEqual(ev.sent, [])

    async def test_explicit_resume_without_quote_accepts_shared_short_id(self):
        ev = event('/codex resume ' + PARENT[:8] + ' 继续看原图', quote=False)
        await self.submit(ev)
        self.assertEqual(self.calls[-1]['parent_job_id'], PARENT)
        self.assertIn('继续看原图', self.calls[-1]['prompt'])

    async def test_foreign_sender_short_label_and_receipt_are_shared(self):
        ev = event(quote_text='Codex 任务编号：' + PARENT[:8])
        parent, _ = self.bridge._reply_parent(ev, allow_task_reference=True)
        self.assertEqual(parent, PARENT)
        self.bridge.routes[PARENT]['reply_message_ids'] = ['public-result-receipt']
        ev = event(quote_text='这是一条没有任务号的结果')
        await self.submit(ev)
        self.assertEqual(self.calls[-1]['parent_job_id'], PARENT)

    async def test_ambiguous_prefix_is_rejected(self):
        self.add_task('a' * 8 + 'c' * 8, OTHER_SCOPE, 'charlie')
        ev = event('/codex resume ' + PARENT[:8] + ' 继续', quote=False)
        response = await self.bridge._submit(ev, ev.message_str, notify=False)
        self.assertFalse(response['ok'])
        await self.bridge._show(ev, 'status', PARENT[:8])
        self.assertIn('不唯一', plain_sent(ev))
        self.assertEqual(self.calls, [])

    async def test_shared_status_uses_other_actors_runner_owner(self):
        ev = event(quote=False)
        await self.bridge._show(ev, 'status', PARENT[:8])
        self.assertIn(PARENT, plain_sent(ev))
        self.assertEqual(self.calls[0]['owner'], owner_key(SCOPE, 'bob'))

    async def test_result_and_image_cross_scope_do_not_redirect_original_delivery(self):
        route = self.add_task(PARENT, PRIVATE, 'bob', artifacts=[{'id': 'image-one', 'mime': 'image/png'}])
        ev = event(quote=False)
        await self.bridge._show(ev, 'result', PARENT[:8])
        self.assertIn('合成共享结果', plain_sent(ev))
        self.assertTrue(any(isinstance(part, Image) for chain in ev.sent for part in chain.chain))
        self.assertEqual([call['op'] for call in self.calls], ['status', 'artifact'])
        self.assertEqual(route['umo'], PRIVATE)
        self.assertFalse(route.get('delivered'))
        self.assertFalse(route.get('acked'))
        self.assertTrue(route['reply_message_ids'])

    async def test_shared_list_and_latest_cover_all_allowed_scopes_only(self):
        self.add_task('c' * 16, PRIVATE, 'charlie', created=2)
        self.add_task('d' * 16, OTHER_SCOPE, 'david', created=3)
        self.add_task('e' * 16, DENIED_SCOPE, 'erin', created=4)
        ev = event(quote=False)
        await self.bridge._show(ev, 'list')
        text = plain_sent(ev)
        for jid in (PARENT, 'c' * 16, 'd' * 16):
            self.assertIn(jid, text)
        self.assertNotIn('e' * 16, text)
        self.calls.clear()
        await self.bridge._show(ev, 'status')
        self.assertEqual(self.calls[-1]['job_id'], 'd' * 16)

    async def test_revoked_scopes_and_unregistered_runner_jobs_cannot_hide_visible_history(self):
        owner = owner_key(SCOPE, 'bob')
        for index in range(2, 8):
            self.add_task(f'{index:016x}', DENIED_SCOPE, 'removed-user', created=index, runner_owner=owner)
        for index in range(8, 14):
            self.jobs[f'{index:016x}'] = {**self.jobs[PARENT], 'id': f'{index:016x}',
                                         'created_at': f'2000-01-01T00:00:{index:02d}+00:00'}
        ev = event(quote=False)
        await self.bridge._show(ev, 'list')
        self.assertIn(PARENT, plain_sent(ev))
        self.assertEqual([(call['op'], call['job_id']) for call in self.calls], [('status', PARENT)])
        self.assertFalse(any(f'{index:016x}' in plain_sent(ev) for index in range(2, 14)))

    async def test_shared_list_fills_missing_records_across_batches_and_stops_after_five(self):
        for index in range(2, 15):
            self.add_task(f'{index:016x}', SCOPE, 'bob', created=index)
        for index in (14, 12, 10, 8):
            self.jobs.pop(f'{index:016x}')
        jobs = await self.bridge._shared_jobs(event(quote=False))
        self.assertEqual([job['id'] for job in jobs], [f'{index:016x}' for index in (13, 11, 9, 7, 6)])
        self.assertEqual(len(self.calls), 10)
        self.assertTrue(all(call['op'] == 'status' for call in self.calls))
        self.assertNotIn(PARENT, {call['job_id'] for call in self.calls})

    async def test_shared_list_all_missing_or_transport_failure_reports_unavailable(self):
        self.jobs.clear()
        ev = event(quote=False)
        await self.bridge._show(ev, 'list')
        self.assertIn('状态服务暂时不可用', plain_sent(ev))
        self.assertNotIn('还没有', plain_sent(ev))
        async def unavailable(payload):
            raise ConnectionError('simulated Unix transport failure')
        self.bridge._rpc = unavailable
        ev = event(quote=False)
        await self.bridge._show(ev, 'list')
        self.assertIn('状态服务暂时不可用', plain_sent(ev))

    async def test_original_sender_filter_does_not_isolate_shared_tasks(self):
        self.bridge.config['allowed_senders'] = ['alice']
        await self.submit(event())
        self.assertEqual(self.calls[-1]['owner'], owner_key(SCOPE, 'bob'))

    async def test_current_sender_filter_still_applies(self):
        self.bridge.config['allowed_senders'] = ['bob']
        ev = event()
        response = await self.bridge._submit(ev, '继续', notify=False)
        self.assertFalse(response['ok'])
        await self.bridge._show(ev, 'list')
        self.assertEqual(self.calls, [])

    async def test_persisted_new_and_legacy_routes_survive_restart(self):
        response = await self.submit(event(scope=OTHER_SCOPE))
        jid = response['job']['id']
        loaded = Main(self.context, self.bridge.config)
        loaded.routes_file = self.bridge.routes_file
        loaded._load()
        self.assertEqual(loaded.routes[jid]['owner'], owner_key(OTHER_SCOPE, 'alice'))
        self.assertEqual(loaded.routes[jid]['runner_owner'], owner_key(SCOPE, 'bob'))
        self.assertEqual(loaded.routes[jid]['runner_workspace'], hashlib.sha256(SCOPE.encode()).hexdigest()[:32])
        self.assertEqual(loaded._runner_owner(loaded.routes[PARENT]), owner_key(SCOPE, 'bob'))
        loaded.routes[jid]['runner_owner'] = 'invalid-owner'
        loaded._save()
        rejected = Main(self.context, self.bridge.config)
        rejected.routes_file = self.bridge.routes_file
        rejected._load()
        self.assertNotIn(jid, rejected.routes)
        self.assertIn(PARENT, rejected.routes)

    async def test_followup_chain_preserves_original_runner_owner_workspace(self):
        response = await self.submit(event(scope=OTHER_SCOPE))
        jid = response['job']['id']
        ev = event(scope=CHARLIE_PRIVATE, sender='charlie', quote_text='【Codex 任务 #' + jid + '】\n上一轮结果')
        response = await self.submit(ev)
        self.assertEqual(self.calls[-1]['parent_job_id'], jid)
        self.assertEqual(self.calls[-1]['owner'], owner_key(SCOPE, 'bob'))
        self.assertEqual(self.calls[-1]['workspace'], hashlib.sha256(SCOPE.encode()).hexdigest()[:32])
        self.assertEqual(self.bridge.routes[response['job']['id']]['owner'], owner_key(CHARLIE_PRIVATE, 'charlie'))

    async def test_pending_tools_results_delivery_and_artifact_translate_only_runner_identity(self):
        response = await self.submit(event())
        route = self.bridge.routes[response['job']['id']]
        for op in ('status', 'tool_pending', 'tool_result', 'delivery', 'artifact'):
            result = await self.bridge._rpc({'op': op, 'owner': route['owner'], 'job_id': route['id']})
            self.assertTrue(result['ok'])
            self.assertEqual(self.calls[-1]['owner'], owner_key(SCOPE, 'bob'))
            self.assertEqual(route['owner'], owner_key(SCOPE, 'alice'))
        result = await self.bridge._rpc({'op': 'status', 'owner': owner_key(SCOPE, 'intruder'), 'job_id': route['id']})
        self.assertFalse(result['ok'])

    async def test_new_direct_image_is_added_to_shared_retained_context(self):
        await self.submit(event('按新图继续分析', direct_image=True))
        self.assertEqual(self.calls[-1]['parent_job_id'], PARENT)
        self.assertEqual(len(self.calls[-1]['images']), 1)
        self.downloader.assert_awaited_once_with(CURRENT_IMAGE)


if __name__ == '__main__':
    unittest.main(verbosity=2)

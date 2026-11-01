import asyncio
from datetime import datetime, timedelta
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from PIL import Image as PillowImage
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.star.star_handler import star_handlers_registry

sys.path.insert(0, os.environ.get('REPAIR_PLUGIN_ROOT', str(Path(__file__).resolve().parents[1] / 'plugins')))
from codex_bridge.main import Main as Codex
from codex_bridge.core import job_key
from image_context.main import Main as ImageContext
from intent_router.main import Main as IntentRouter
from long_term_memory.main import Main as Memory
from reminder.main import Main as Reminder
from xhs_search.main import Main as XHS

TZ = ZoneInfo('Asia/Shanghai')
checks = []


def event(text, *, private=False, sender='synthetic-owner', group='synthetic-group', message_id='synthetic-message', reply=None):
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE if private else MessageType.GROUP_MESSAGE
    message.message = [Plain(text)] + ([reply] if reply else [])
    message.message_str = text
    message.sender = MessageMember(sender)
    message.group_id = '' if private else group
    message.self_id = 'synthetic-bot'
    message.message_id = message_id
    source = AstrMessageEvent(text, message, PlatformMetadata('qq_official', 'test', 'test'), sender if private else group)
    source.is_at_or_wake_command = True
    source.send = AsyncMock()
    return source


def sent_text(source):
    return '\n'.join(part.text for call in source.send.call_args_list for part in call.args[0].chain if isinstance(part, Plain))


def passed(name):
    checks.append(name)
    print('PASS ' + name, flush=True)


async def invoke(tool, source, **kwargs):
    values = []
    context = ContextWrapper(SimpleNamespace(event=source))
    async for value in FunctionToolExecutor.execute(tool, context, **kwargs):
        if value is not None:
            values.extend(part.text for part in value.content if part.type == 'text')
    assert len(values) == 1, 'framework tool invocation returned wrong content count'
    return json.loads(values[0])


async def main():
    with tempfile.TemporaryDirectory(prefix='history-runtime-', dir=os.environ['REPAIR_STATE_ROOT']) as directory:
        root = Path(directory)
        group_umo = event('').unified_msg_origin
        private_umo = event('', private=True).unified_msg_origin
        context = SimpleNamespace(send_message=AsyncMock(return_value=True), get_all_stars=Mock(return_value=[]))
        xhs = XHS(context, {'allowed_umos': [group_umo, private_umo], 'login_umos': [private_umo], 'login_senders': ['synthetic-owner']})
        for phrase in ('已登录，检查下小红书的登录态', '我已经扫码登录了，你检查下状态', '我已经扫码确认了'):
            source = event(phrase, private=True)
            xhs._pending_login[xhs._event_identity(source)] = float('inf')
            with patch.object(xhs, '_call', new=AsyncMock(return_value={'is_logged_in': True})) as call:
                await xhs.natural_login(source)
            call.assert_awaited_once_with('login_details')
            assert source.is_stopped() and '已登录' in sent_text(source)
            assert '图片' not in sent_text(source) and 'Computer Use' not in sent_text(source)
        passed('historical XHS login confirmations use local status, not Codex')
        source = event('小红书现在可以用吗', private=True)
        req = ProviderRequest(prompt=source.message_str)
        await xhs.provide_search_tool(source, req)
        with patch.object(xhs, '_call', new=AsyncMock(return_value={'is_logged_in': False, 'login_pending': True})):
            result = await invoke(req.func_tool.get_tool('xhs_login_status'), source)
        assert result['state'] == 'waiting_for_scan'
        source = event('小红书搜索南京剧本杀', private=True)
        source.is_at_or_wake_command = False
        with patch.object(xhs, '_call', new=AsyncMock(return_value={'results': [], 'references': {}})) as call:
            await xhs.natural_search(source)
        call.assert_awaited_once_with('search_bundle', '南京剧本杀', limit=5)
        denied = event('检查小红书登录态', private=True, sender='synthetic-other')
        with patch.object(xhs, '_call', new=AsyncMock()) as call:
            await xhs.natural_login(denied)
        call.assert_not_awaited()
        passed('real AstrBot XHS tool executor, private search and ownership boundaries')

        selector = Mock(side_effect=ValueError('synthetic cache miss'))
        context.get_all_stars.return_value = [SimpleNamespace(name='image_context', activated=True, star_cls=SimpleNamespace(select_for_task=selector))]
        codex = Codex(context, {'allowed_umos': [group_umo, private_umo]})
        codex.routes_file = root / 'codex-jobs.json'
        def accepted(payload):
            return {'ok': True, 'job': {'id': payload['job_id'], 'state': 'queued', 'mode': 'read-only', 'phase': 'queued', 'created_at': datetime.now(TZ).isoformat(), 'parent_job_id': payload.get('parent_job_id'), 'conversation_id': payload.get('parent_job_id') or payload['job_id'], 'resumable': True, 'context_images': 0}}
        codex._rpc = AsyncMock(side_effect=accepted)
        source = event('用 codex 看下为什么不可用', private=True)
        await codex.natural_status(source)
        codex._rpc.assert_awaited_once()
        assert '已受理' in sent_text(source) and source.is_stopped()
        selector.assert_not_called()
        passed('historical plain-text Codex task bypasses image cache and submits read-only')
        codex._rpc.reset_mock()
        source = event('请系统核对三种储能方案并列出各自的证据和成本', message_id='semantic')
        req = ProviderRequest(prompt=source.message_str)
        await codex.provide_delegation_tool(source, req)
        result = await invoke(req.func_tool.get_tool('delegate_current_task_to_codex'), source)
        assert result['ok'] and 'status_board' in result
        codex._rpc.assert_awaited_once()
        selector.assert_not_called()
        await codex.describe_available_capabilities(source, req)
        assert '本条请求的真实工具清单' in req.system_prompt
        passed('real AstrBot Codex semantic tool executor and current capability manifest')
        parent = next(iter(codex.routes.values()))
        codex._rpc.reset_mock()
        async def followup_rpc(payload):
            if payload['op'] == 'status':
                return {'ok': True, 'job': {'id': parent['id'], 'state': 'completed', 'mode': 'read-only', 'conversation_id': parent['id'], 'resumable': True, 'context_images': 1}}
            return accepted(payload)
        codex._rpc = AsyncMock(side_effect=followup_rpc)
        source = event('重新提交', private=True, message_id='retry', reply=Reply(id='synthetic-result', message_str=f'【Codex 任务 #{parent["id"]}】'))
        await codex.natural_status(source)
        assert codex._rpc.call_args.args[0]['parent_job_id'] == parent['id']
        assert '同会话续接' in sent_text(source)
        passed('historical retry retains original owner-scoped Codex conversation')
        for phrase in ('测试一下 at 我', '请@我一下'):
            source = event(phrase, message_id='mention')
            await codex.mention_current_sender(source)
            chain = source.send.call_args.args[0]
            assert source.is_stopped() and chain.use_markdown_
            assert isinstance(chain.chain[0], At) and chain.chain[0].qq == source.get_sender_id()
        passed('historical mention request uses current sender and QQ Markdown At')

        reminder_config = {'allowed_umos': [group_umo], 'reminder_file': str(root / 'reminders.json')}
        reminder = Reminder(context, reminder_config)
        for index, phrase in enumerate(('一分钟后提醒我吃饭', '10s后提醒我吃饭')):
            source = event(phrase, message_id=f'reminder-{index}')
            await reminder.natural_reminder(source)
            assert source.is_stopped() and len(reminder._reminders) == index + 1
        restored = Reminder(context, reminder_config)
        restored._load()
        assert len(restored._reminders) == 2
        for item in reminder._reminders:
            item['due_at'] = 0
        async def one_tick(seconds):
            if seconds == 1:
                reminder._closed = True
        with patch('reminder.main.asyncio.sleep', one_tick):
            await reminder._scheduler()
        assert not reminder._reminders
        assert context.send_message.await_count == 2
        assert all(call.args[1].use_markdown_ and isinstance(call.args[1].chain[0], At) for call in context.send_message.await_args_list)
        passed('historical second/minute reminders persist and deliver with At via fake transport')

        source = event('记住我明天上午七点到医院，要吃面。明天中午约咖啡。周六早上九点半打乒乓球。分别创建提醒', message_id='multi')
        memory = Memory(context, {'allowed_umos': [group_umo], 'memory_file': str(root / 'memory.sqlite3')})
        await memory.initialize()
        await memory.explicit_memory_request(source)
        assert not source.is_stopped() and not memory._rows(memory._owner(source))
        req = ProviderRequest(prompt=source.message_str)
        await reminder.provide_schedule_tool(source, req)
        assert req.func_tool.get_tool('create_schedule_reminders')
        bad = await invoke(req.func_tool.get_tool('create_schedule_reminders'), source, items=[{'text': '约咖啡', 'event_at': '明天中午'}], advance_minutes=0)
        assert not bad['ok'] and not reminder._reminders
        tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=7, minute=0, second=0, microsecond=0)
        good_source = event('请创建两条提醒，明天早上七点吃早餐，八点出门', message_id='multi-valid')
        good_req = ProviderRequest(prompt=good_source.message_str)
        await reminder.provide_schedule_tool(good_source, good_req)
        tool = good_req.func_tool.get_tool('create_schedule_reminders')
        items = [{'text': '吃早餐', 'event_at': tomorrow.isoformat()}, {'text': '出门', 'event_at': (tomorrow + timedelta(hours=1)).isoformat()}]
        good = await invoke(tool, good_source, items=items, advance_minutes=0)
        assert good['ok'] and len(good['created']) == 2
        assert await invoke(tool, good_source, items=items, advance_minutes=0) == good
        assert len(reminder._reminders) == 2
        passed('multi-reminders defer from memory, clarify invalid time and remain idempotent')

        source = event('按照我刚发的图，为我创建提醒事项，提前十分钟提醒我', message_id='image-schedule')
        source.message_obj.message.append(Image.fromURL('https://multimedia.nt.qq.com.cn/download?fileid=synthetic-schedule'))
        req = ProviderRequest(prompt=source.message_str, image_urls=['synthetic-loaded-schedule'])
        await reminder.provide_schedule_tool(source, req)
        tool = req.func_tool.get_tool('create_schedule_reminders')
        future_items = [{'text': '合成日程', 'event_at': tomorrow.replace(hour=18).isoformat()}]
        result = await invoke(tool, source, items=future_items, advance_minutes=10)
        assert result['ok'] and len(result['created']) == 1
        actual = next(item for item in reminder._reminders if item['text'] == '合成日程')
        assert actual['event_at'] - actual['due_at'] == 600
        passed('image schedule tool persists the original time and subtracts lead time exactly once')

        source = event('按照图片创建提醒，提前十分钟', message_id='expired-schedule')
        req = ProviderRequest(prompt=source.message_str)
        await reminder.provide_schedule_tool(source, req)
        old_time = (datetime.now(TZ) - timedelta(hours=1)).replace(microsecond=0)
        old_items = [{'text': '合成过期日程', 'event_at': old_time.isoformat()}]
        previous_count = len(reminder._reminders)
        result = await invoke(req.func_tool.get_tool('create_schedule_reminders'), source, items=old_items, advance_minutes=10)
        assert not result['ok'] and len(reminder._reminders) == previous_count
        foreign = event('改成明天相同时间', sender='synthetic-other', message_id='foreign-shift')
        await reminder.natural_reminder(foreign)
        assert len(reminder._reminders) == previous_count
        followup = event('改成明天相同时间', message_id='valid-shift')
        await reminder.natural_reminder(followup)
        assert followup.is_stopped() and '已按明天相同时间创建' in sent_text(followup)
        shifted = next(item for item in reminder._reminders if item['text'] == '合成过期日程')
        assert shifted['event_at'] == (old_time + timedelta(days=1)).timestamp()
        assert shifted['event_at'] - shifted['due_at'] == 600
        passed('expired image schedule stays uncreated until the same owner explicitly requests tomorrow')

        image_context = ImageContext(context, {'allowed_umos': [group_umo]})
        reference = 'https://multimedia.nt.qq.com.cn/download?fileid=synthetic-fresh-image'
        upload = event('', message_id='fresh-image')
        upload.is_at_or_wake_command = False
        upload.message_obj.message.append(Image.fromURL(reference))
        await image_context.remember_images(upload)
        followup = event('看看我刚发的图片', message_id='image-followup')
        req = ProviderRequest(prompt=followup.message_str)
        await image_context.add_image_context(followup, req)
        assert any(getattr(getattr(part, 'image_url', None), 'url', None) == reference for part in req.extra_user_content_parts)
        foreign = event('看看我刚发的图片', sender='synthetic-other', message_id='foreign-image')
        foreign_req = ProviderRequest(prompt=foreign.message_str)
        await image_context.add_image_context(foreign, foreign_req)
        assert foreign.is_stopped() and not foreign_req.extra_user_content_parts
        await image_context.terminate()
        passed('fresh image follow-up loads original attachment only for the same group and sender')

        source = event('将当前日期和该报告单记入长期记忆，备查', message_id='report')
        req = ProviderRequest(prompt=source.message_str, image_urls=['synthetic-loaded-report'])
        await memory.add_memory_context(source, req)
        result = await invoke(req.func_tool.get_tool('manage_long_term_memory'), source, action='add', content='合成测试报告：检查日期2026-10-30；检测指标A为1.0，参考范围0至2。')
        assert result['ok'] and '记录日期：' in memory._rows(memory._owner(source))[0][1]
        missing = event(source.message_str, reply=Reply(id='placeholder', message_str='[图片]'))
        missing_req = ProviderRequest(prompt=missing.message_str)
        await memory.add_memory_context(missing, missing_req)
        assert missing_req.func_tool is None
        await memory.terminate()
        passed('report memory saves actual authorized summary/date, rejects placeholder-only quote')

        router = IntentRouter(context, {'allowed_umos': [group_umo], 'mode': 'light'})
        payload = {'route': 'xhs_status', 'confidence': 0.99, 'task': '检查小红书登录态', 'mode': 'read-only', 'missing_context': []}
        with patch.object(router, '_resolve_provider', new=AsyncMock(return_value=SimpleNamespace(text_chat=AsyncMock(return_value=SimpleNamespace(completion_text=json.dumps(payload)))))):
            source = event('检查小红书登录态')
            await router.classify_group_message(source)
        assert source.get_extra('intent_router_route') == 'xhs_status'
        await codex.natural_status(source)
        assert not source.is_stopped()
        metadata = list(star_handlers_registry)
        priorities = {item.handler_name: item.extras_configs.get('priority') for item in metadata if item.handler_name in {'classify_group_message', 'natural_login', 'natural_status', 'add_memory_context', 'add_image_context'}}
        assert priorities['classify_group_message'] > priorities['natural_login'] > priorities['natural_status']
        assert priorities['add_image_context'] > priorities['add_memory_context']
        passed('real registration orders planner/local XHS before Codex and images before report memory')
    target = Path(os.environ['REPAIR_STATE_ROOT']) / 'history-runtime.json'
    target.write_text(json.dumps({'ok': True, 'checks': checks}, ensure_ascii=False, indent=2))
    target.chmod(0o600)
    print(json.dumps({'ok': True, 'checks': len(checks), 'outbound_qq': 0, 'production_memory_writes': 0, 'production_reminder_writes': 0}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())

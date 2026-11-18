"""Synthetic events in installed AstrBot; no QQ, ASR model or provider calls."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = tempfile.TemporaryDirectory(prefix='voice-framework-runtime-')
os.environ['ASTRBOT_ROOT'] = SANDBOX.name
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

from astrbot.core import astrbot_config
from astrbot.api.message_components import At, Plain, Record, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import QQOfficialPlatformAdapter

SPEC = importlib.util.spec_from_file_location('portable_voice_runtime', ROOT / 'patches/astrbot-voice/voice_input.py')
voice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice)


def event(*, reply=False, denied=False, mid='synthetic-voice-message'):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.group_id = 'denied-group' if denied else 'allowed-group'
    message.sender = MessageMember('synthetic-owner')
    message.self_id = 'synthetic-bot'
    message.message_id = mid
    message.message_str = ''
    record = Record(file='https://multimedia.nt.qq.com/synthetic.audio')
    quoted = Reply(id='synthetic-original-reply', message_str='quoted context', chain=[Plain('quoted context'), record])
    message.message = [At(qq='synthetic-bot'), quoted if reply else record]
    result = AstrMessageEvent('', message, PlatformMetadata('qq_official', 'voice-test', 'synthetic'), message.group_id)
    result.is_at_or_wake_command = True
    result.timeline = []
    async def send(chain):
        assert not result.get_extra('voice_input_transcribed'), 'dispatch became visible before the echo receipt'
        result.timeline.append('echo' if any(isinstance(part, Plain) and part.text.startswith('语音识别：') for part in chain.chain) else 'error')
        chain.qqofficial_sent_message_ids = ['synthetic-delivery']
        result.set_extra('qqofficial_sent_message_ids', ['synthetic-delivery'])
        result._has_send_oper = True
        return True
    result.send = send
    return result


async def main():
    report = {'ok': False, 'real_astrbot_events': True, 'network_calls': 0, 'qq_messages_sent': 0, 'checks': []}
    state = Path(SANDBOX.name) / 'voice-input'
    state.mkdir()
    (state / 'config.json').write_text('{"enabled":true}')
    source = event()
    config = copy.deepcopy(dict(astrbot_config))
    config['platform_settings'].update(enable_id_white_list=True, id_whitelist=[source.unified_msg_origin])
    async def download(unused_url, path):
        path.write_bytes(b'synthetic audio; transport is intercepted')
    recognizer = AsyncMock(return_value={'ok': True, 'text': '/codex 分析合成项目', 'duration_seconds': 1})
    pipeline = voice.VoiceInput(config, state_directory=state, downloader=download, recognizer=recognizer)
    try:
        assert await pipeline.process(source, lambda text: (True, ''))
        assert source.timeline == ['echo'] and source.message_str == '/codex 分析合成项目'
        assert source.message_obj.message_str == source.message_str and not source._has_send_oper
        assert source.get_extra('qqofficial_sent_message_ids') is None
        assert source.get_extra('voice_input_transcribed') is True
        report['checks'].append('real event receives a confirmed echo before transcription is exposed; business send state is restored')

        quoted = event(reply=True, mid='synthetic-quoted-voice')
        reply = next(part for part in quoted.get_messages() if isinstance(part, Reply))
        assert await pipeline.process(quoted, lambda text: (True, ''))
        assert reply in quoted.get_messages() and reply.id == 'synthetic-original-reply'
        assert quoted.message_str == '/codex 分析合成项目'
        assert isinstance(reply.chain[1], Plain) and 'quoted context' not in quoted.message_str
        report['checks'].append('direct reply audio is transcribed while reply identity remains available and quoted prose stays context')

        count = recognizer.await_count
        denied = event(denied=True, mid='synthetic-denied')
        assert not await pipeline.process(denied, lambda text: (True, ''))
        assert denied.is_stopped() and denied.timeline == [] and recognizer.await_count == count
        unsafe = event(mid='synthetic-unsafe')
        assert not await pipeline.process(unsafe, lambda text: (False, 'synthetic unsafe content'))
        assert unsafe.is_stopped() and 'echo' not in unsafe.timeline and not unsafe.get_extra('voice_input_transcribed')
        report['checks'].append('scope and content-safety checks stop voice dispatch without a model or business command')

        components = []
        with patch.object(QQOfficialPlatformAdapter, '_prepare_audio_attachment', wraps=QQOfficialPlatformAdapter._prepare_audio_attachment) as prepare:
            await QQOfficialPlatformAdapter._append_attachments(components, [
                {'content_type': 'audio/silk', 'filename': 'no-extension', 'url': 'https://multimedia.nt.qq.com/synthetic.audio'}])
            prepare.assert_awaited_once()
        assert len(components) == 1 and isinstance(components[0], Record)
        assert components[0].file == 'https://multimedia.nt.qq.com/synthetic.audio'
        assert not list((state / 'tmp').iterdir())
        report['checks'].append('real QQ adapter defers audio download and conversion until the authorized voice stage')
        report['ok'] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        SANDBOX.cleanup()


if __name__ == '__main__':
    asyncio.run(main())

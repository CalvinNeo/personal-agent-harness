"""Voice transport and event boundaries with synthetic input and no network."""
from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('voice_input_under_test', ROOT / 'patches/astrbot-voice/voice_input.py')
voice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voice)
SCOPE = 'synthetic:GroupMessage:group'
AUDIO = 'https://multimedia.nt.qq.com/synthetic.audio'


class Plain:
    def __init__(self, text): self.text = text


class Record:
    def __init__(self, url=AUDIO): self.url = self.file = url


class Reply:
    def __init__(self, chain): self.chain, self.message_str = chain, ''


class Result:
    def message(self, text): self.chain = [Plain(text)]; return self
    def use_markdown(self, unused): return self


class Event:
    def __init__(self, parts=None, *, scope=SCOPE, mid='synthetic-message'):
        self.unified_msg_origin = scope
        self.message_obj = SimpleNamespace(message_id=mid, message=parts if parts is not None else [Record()], message_str='')
        self.message_str = ''
        self.is_at_or_wake_command = True
        self.extras = {'qqofficial_sent_message_ids': ['earlier-receipt']}
        self._has_send_oper = False
        self.timeline, self.sent = [], []
        self.stopped = False
    def get_messages(self): return self.message_obj.message
    def get_extra(self, key=None, default=None): return self.extras if key is None else self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_platform_name(self): return 'qq_official'
    def get_platform_id(self): return 'synthetic'
    def get_sender_id(self): return 'synthetic-owner'
    def get_group_id(self): return 'group'
    def is_private_chat(self): return ':FriendMessage:' in self.unified_msg_origin
    def stop_event(self): self.stopped = True
    async def send(self, chain):
        self.sent.append(chain)
        self.timeline.append('echo' if chain.chain[0].text.startswith('语音识别：') else 'error')
        chain.qqofficial_sent_message_ids = ['synthetic-receipt']
        self.extras['qqofficial_sent_message_ids'] = ['synthetic-receipt']
        self._has_send_oper = True
        return True


def framework_fakes():
    modules = {name: ModuleType(name) for name in ('astrbot', 'astrbot.core.message.components', 'astrbot.core.message.message_event_result')}
    modules['astrbot'].logger = logging.getLogger('synthetic-voice-test')
    for name, value in (('Plain', Plain), ('Record', Record), ('Reply', Reply)):
        setattr(modules['astrbot.core.message.components'], name, value)
    modules['astrbot.core.message.message_event_result'].MessageEventResult = Result
    return patch.dict('sys.modules', modules)


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        (self.state / 'config.json').write_text('{"enabled":true}')
        self.config = {'platform_settings': {'enable_id_white_list': True, 'id_whitelist': [SCOPE]}}
        self.downloader = AsyncMock(side_effect=lambda unused, target: target.write_bytes(b'synthetic audio'))
        self.recognizer = AsyncMock(return_value={'ok': True, 'text': '/codex 分析合成项目', 'duration_seconds': 1})
        self.pipeline = voice.VoiceInput(self.config, state_directory=self.state, downloader=self.downloader, recognizer=self.recognizer)
        self.framework = framework_fakes()
        self.framework.start()
    async def asyncTearDown(self):
        self.framework.stop()
        self.temp.cleanup()

    def test_only_approved_qq_audio_urls_are_accepted(self):
        self.assertEqual(voice.validate_audio_url(AUDIO), AUDIO)
        for url in ('file:///tmp/audio', 'https://example.com/audio', 'https://qq.com.example.org/audio',
                    'https://user:pass@qq.com/audio', AUDIO + '#fragment', 'https://qq.com:8443/audio', AUDIO + '\n'):
            with self.subTest(url=url), self.assertRaises(voice.VoiceInputError):
                voice.validate_audio_url(url)

    async def test_mixed_public_and_private_dns_addresses_fail_closed(self):
        resolver = voice.PublicAudioResolver()
        resolver.resolver = SimpleNamespace(resolve=AsyncMock(return_value=[{'host': '192.0.2.10'}]), close=AsyncMock())
        with self.assertRaisesRegex(voice.VoiceInputError, 'invalid_audio'):
            await resolver.resolve('multimedia.nt.qq.com', 443)
        await resolver.close()

    async def test_transcript_echo_finishes_before_current_event_is_updated(self):
        event = Event()
        async def deliver(chain):
            self.assertFalse(event.get_extra('voice_input_transcribed'))
            self.assertIsInstance(event.get_messages()[0], Record)
            await Event.send(event, chain)
        event.send = deliver
        self.assertTrue(await self.pipeline.process(event, lambda text: (True, '')))
        self.assertEqual(event.timeline, ['echo'])
        self.assertEqual(event.message_str, '/codex 分析合成项目')
        self.assertEqual(event.message_obj.message_str, event.message_str)
        self.assertIsInstance(event.get_messages()[0], Plain)
        self.assertTrue(event.get_extra('voice_input_transcribed'))
        self.assertFalse(event._has_send_oper)
        self.assertEqual(event.get_extra('qqofficial_sent_message_ids'), ['earlier-receipt'])
        self.assertFalse(list((self.state / 'tmp').iterdir()))

    async def test_direct_reply_voice_keeps_reply_identity_and_excludes_other_quoted_prose(self):
        reply = Reply([Plain('quoted instructions stay context'), Record()])
        event = Event([Plain('请检查'), reply])
        self.assertTrue(await self.pipeline.process(event, lambda text: (True, '')))
        self.assertIs(event.get_messages()[1], reply)
        self.assertEqual(event.message_str, '请检查\n/codex 分析合成项目')
        self.assertNotIn('quoted instructions', event.message_str)
        self.assertIsInstance(reply.chain[1], Plain)

    async def test_unauthorized_disabled_and_duplicate_input_never_execute_recognizer_twice(self):
        denied = Event(scope='synthetic:GroupMessage:denied')
        self.assertFalse(await self.pipeline.process(denied, lambda text: (True, '')))
        self.downloader.assert_not_awaited()
        (self.state / 'config.json').write_text('{"enabled":false}')
        self.assertFalse(await self.pipeline.process(Event(), lambda text: (True, '')))
        self.downloader.assert_not_awaited()
        (self.state / 'config.json').write_text('{"enabled":true}')
        self.assertTrue(await self.pipeline.process(Event(), lambda text: (True, '')))
        repeated = Event()
        self.assertFalse(await self.pipeline.process(repeated, lambda text: (True, '')))
        self.assertEqual(self.recognizer.await_count, 1)
        self.assertEqual(repeated.sent, [])

    async def test_multiple_audio_segments_safety_failure_and_echo_failure_stop_dispatch(self):
        multiple = Event([Record(), Record()])
        self.assertFalse(await self.pipeline.process(multiple, lambda text: (True, '')))
        self.downloader.assert_not_awaited()
        unsafe = Event(mid='synthetic-unsafe')
        self.assertFalse(await self.pipeline.process(unsafe, lambda text: (False, 'synthetic rejection')))
        self.assertNotIn('echo', unsafe.timeline)
        self.assertFalse(unsafe.get_extra('voice_input_transcribed'))
        failed = Event(mid='synthetic-echo-failed')
        failed.send = AsyncMock(return_value=False)
        self.assertFalse(await self.pipeline.process(failed, lambda text: (True, '')))
        self.assertFalse(failed.get_extra('voice_input_transcribed'))
        self.assertTrue(failed.stopped)

    async def test_recognition_client_bounds_and_validates_real_wire_response(self):
        writer = SimpleNamespace(write=lambda data: None, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock())
        reader = SimpleNamespace(readline=AsyncMock(return_value=json.dumps({'ok': True, 'text': '合成\u0000转写'}).encode()))
        with patch.object(voice.asyncio, 'open_unix_connection', new=AsyncMock(return_value=(reader, writer))):
            result = await voice.recognize_audio('voice-synthetic/input.audio')
        self.assertEqual(result['text'], '合成转写')
        for response in ({'ok': True, 'text': '!' }, {'ok': True, 'text': 'x' * 3001}, {'ok': False, 'code': 'do not expose arbitrary server diagnostics'}):
            reader.readline = AsyncMock(return_value=json.dumps(response).encode())
            with patch.object(voice.asyncio, 'open_unix_connection', new=AsyncMock(return_value=(reader, writer))), self.assertRaises(voice.VoiceInputError):
                await voice.recognize_audio('voice-synthetic/input.audio')

    def test_transcribed_event_text_has_priority_over_a_retained_older_fragment(self):
        event = Event()
        event.set_extra('voice_input_transcribed', True)
        event.set_extra('voice_input_transcript', 'older transcript')
        event.message_str = 'current complete transcript'
        self.assertEqual(voice.get_voice_input_text(event), 'current complete transcript')
        event.set_extra('voice_input_transcribed', False)
        self.assertEqual(voice.get_voice_input_text(event), '')


if __name__ == '__main__':
    unittest.main()

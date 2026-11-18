from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unicodedata
from urllib.parse import urljoin, urlsplit

import aiohttp


STATE_DIRECTORY = Path(os.environ.get('ASTRBOT_VOICE_STATE_DIRECTORY', '/var/lib/astrbot/data/voice-input'))
SOCKET_PATH = os.environ.get('ASTRBOT_VOICE_SOCKET', '/run/astrbot-voice.sock')
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 60
MAX_TEXT_CHARS = 3000
DOWNLOAD_TIMEOUT = 20
RECOGNITION_TIMEOUT = 70
ALLOWED_AUDIO_DOMAINS = ('qq.com', 'qq.com.cn', 'qpic.cn', 'qlogo.cn', 'gtimg.cn', 'gtimg.com')
_recognition_lock = asyncio.Lock()


def get_voice_input_text(event) -> str:
    """Return the current event text after voice transcription, when present."""
    if not event.get_extra('voice_input_transcribed'):
        return ''
    text = str(getattr(event, 'message_str', '') or '').strip()
    if text:
        return text
    return str(event.get_extra('voice_input_transcript') or '').strip()


class VoiceInputError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_audio_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise VoiceInputError('invalid_audio')
    if any(ord(character) < 32 for character in value):
        raise VoiceInputError('invalid_audio')
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or '').lower()
        permitted = any(hostname == domain or hostname.endswith('.' + domain) for domain in ALLOWED_AUDIO_DOMAINS)
        if (parsed.scheme not in {'https', 'http'} or not permitted
                or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in {None, 443 if parsed.scheme == 'https' else 80}):
            raise VoiceInputError('invalid_audio')
    except ValueError:
        raise VoiceInputError('invalid_audio') from None
    return value


class PublicAudioResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await self.resolver.resolve(host, port, family)
        if not addresses:
            raise VoiceInputError('download_failed')
        for address in addresses:
            resolved = ipaddress.ip_address(address['host'])
            if not resolved.is_global or resolved.is_multicast:
                raise VoiceInputError('invalid_audio')
        return addresses

    async def close(self):
        await self.resolver.close()


async def download_audio(url: str, destination: Path) -> None:
    current = validate_audio_url(url)
    resolver = PublicAudioResolver()
    try:
        connector = aiohttp.TCPConnector(resolver=resolver)
        async with aiohttp.ClientSession(
            connector=connector, trust_env=False, auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT),
            headers={'Accept-Encoding': 'identity'},
        ) as session:
            for redirect_count in range(4):
                async with session.get(current, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        if redirect_count == 3:
                            raise VoiceInputError('download_failed')
                        current = validate_audio_url(urljoin(current, response.headers.get('Location', '')))
                        continue
                    if response.status != 200 or response.headers.get('Content-Encoding', 'identity') != 'identity':
                        raise VoiceInputError('download_failed')
                    if response.content_length is not None and response.content_length > MAX_AUDIO_BYTES:
                        raise VoiceInputError('too_large')
                    received = 0
                    with destination.open('xb') as output:
                        destination.chmod(0o600)
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            received += len(chunk)
                            if received > MAX_AUDIO_BYTES:
                                raise VoiceInputError('too_large')
                            output.write(chunk)
                    if not received:
                        raise VoiceInputError('empty_audio')
                    return
            raise VoiceInputError('download_failed')
    finally:
        await resolver.close()


def claim_message(event, state_directory: Path) -> bool:
    message_id = str(event.message_obj.message_id or '').strip()
    if not message_id or len(message_id) > 512:
        raise VoiceInputError('invalid_message')
    identity = '\0'.join((str(event.get_platform_id()), str(event.unified_msg_origin),
                          str(event.get_sender_id()), message_id))
    identity_hash = hashlib.sha256(identity.encode()).hexdigest()
    database = state_directory / 'seen.sqlite3'
    now = int(time.time())
    with sqlite3.connect(database, timeout=0.2) as connection:
        database.chmod(0o600)
        connection.execute('CREATE TABLE IF NOT EXISTS seen (identity TEXT PRIMARY KEY, created INTEGER NOT NULL)')
        connection.execute('DELETE FROM seen WHERE created < ?', (now - 86400,))
        if connection.execute('SELECT 1 FROM seen WHERE identity = ?', (identity_hash,)).fetchone():
            return False
        if connection.execute('SELECT COUNT(*) FROM seen').fetchone()[0] >= 10000:
            raise VoiceInputError('busy')
        return connection.execute('INSERT OR IGNORE INTO seen VALUES (?, ?)', (identity_hash, now)).rowcount == 1


async def recognize_audio(relative_file: str, socket_path: str = SOCKET_PATH) -> dict:
    writer = None
    try:
        async with asyncio.timeout(RECOGNITION_TIMEOUT):
            reader, writer = await asyncio.open_unix_connection(socket_path, limit=32768)
            writer.write((json.dumps({'file': relative_file}) + '\n').encode())
            await writer.drain()
            response = await reader.readline()
            if not response or len(response) > 32768:
                raise VoiceInputError('recognition_failed')
            result = json.loads(response)
    except (ValueError, OSError, asyncio.TimeoutError):
        raise VoiceInputError('recognition_failed') from None
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
    if not isinstance(result, dict) or result.get('ok') is not True:
        code = result.get('code') if isinstance(result, dict) else None
        raise VoiceInputError(code if code in {'too_long', 'too_large', 'empty_audio', 'unsupported_audio', 'busy'} else 'recognition_failed')
    text = result.get('text')
    if not isinstance(text, str):
        raise VoiceInputError('empty_audio')
    text = ''.join(character for character in text if unicodedata.category(character)[0] != 'C' or character in '\n\t').strip()
    if not text or not any(character.isalnum() for character in text):
        raise VoiceInputError('empty_audio')
    if len(text) > MAX_TEXT_CHARS:
        raise VoiceInputError('too_long')
    result['text'] = text
    return result


def allowed_voice_event(event, config: dict) -> bool:
    if event.get_platform_name() != 'qq_official' or not event.is_at_or_wake_command:
        return False
    settings = config.get('platform_settings', {})
    allowed = {str(value).strip() for value in settings.get('id_whitelist', []) if str(value).strip()}
    if event.is_private_chat():
        return bool(settings.get('enable_id_white_list') and str(event.unified_msg_origin) in allowed)
    return bool(settings.get('enable_id_white_list') and allowed
                and (str(event.unified_msg_origin) in allowed or str(event.get_group_id()).strip() in allowed))


class VoiceInput:
    def __init__(self, config: dict, *, state_directory: Path = STATE_DIRECTORY,
                 downloader=download_audio, recognizer=recognize_audio):
        self.config = config
        self.state_directory = state_directory
        self.downloader = downloader
        self.recognizer = recognizer

    async def _stop(self, event, code: str, *, notify: bool = True) -> bool:
        from astrbot import logger
        from astrbot.core.message.message_event_result import MessageEventResult

        messages = {
            'disabled': '语音输入暂未启用，本条消息未执行。',
            'too_many': '请每条消息只发一段语音，本条消息未执行。',
            'too_large': '语音文件超过 8 MiB，请缩短后重发；本条消息未执行。',
            'too_long': '语音请控制在 60 秒以内，本条消息未执行。',
            'empty_audio': '没有识别到清晰的语音文字，请重发或直接打字；本条消息未执行。',
            'busy': '正在识别另一条语音，请稍后重发；本条消息未执行。',
            'unsafe': '语音文字未通过内容安全检查，本条消息未执行。',
            'unsupported_audio': '暂时无法解码这段语音，请重发 QQ 语音或直接打字；本条消息未执行。',
        }
        event.stop_event()
        if code not in {'duplicate', 'denied'}:
            logger.warning('voice_input stopped reason=%s', code)
        if notify:
            try:
                message = messages.get(code, '语音识别失败，请稍后重发或直接打字；本条消息未执行。')
                await asyncio.wait_for(event.send(MessageEventResult().message(message).use_markdown(False)), timeout=10)
            except Exception:
                logger.warning('voice_input error notification failed')
        return False

    async def process(self, event, safety_check) -> bool:
        from astrbot import logger
        from astrbot.core.message.components import Plain, Record, Reply
        from astrbot.core.message.message_event_result import MessageEventResult

        if event.get_extra('voice_input_processed'):
            return await self._stop(event, 'duplicate', notify=False)
        # QQ puts media from a quoted message inside Reply.chain.  Keep the
        # Reply component intact so downstream handlers can still bind the
        # quoted message, but treat its direct Record as the user's voice
        # input just like a top-level Record.  Nested replies remain opaque.
        records = []
        locations = []
        for index, component in enumerate(event.get_messages()):
            if isinstance(component, Record):
                records.append(component)
                locations.append(("top", index, None))
            elif isinstance(component, Reply) and component.chain:
                for nested_index, nested in enumerate(component.chain):
                    if isinstance(nested, Record):
                        records.append(nested)
                        locations.append(("reply", index, nested_index))
        if not records:
            return True
        if not allowed_voice_event(event, self.config):
            return await self._stop(event, 'denied', notify=False)
        event.set_extra('voice_input_processed', True)
        if len(records) != 1:
            return await self._stop(event, 'too_many')
        try:
            settings = json.loads((self.state_directory / 'config.json').read_text())
            if settings.get('enabled') is not True:
                return await self._stop(event, 'disabled')
            if not claim_message(event, self.state_directory):
                return await self._stop(event, 'duplicate', notify=False)
            url = validate_audio_url(records[0].url or records[0].file)
            if _recognition_lock.locked():
                return await self._stop(event, 'busy')
            async with _recognition_lock:
                temporary_root = self.state_directory / 'tmp'
                temporary_root.mkdir(mode=0o700, exist_ok=True)
                async with asyncio.timeout(DOWNLOAD_TIMEOUT + RECOGNITION_TIMEOUT + 2):
                    with tempfile.TemporaryDirectory(prefix='voice-', dir=temporary_root) as temporary:
                        audio = Path(temporary) / 'input.audio'
                        await self.downloader(url, audio)
                        result = await self.recognizer(str(audio.relative_to(temporary_root)))
            transcript = result['text']
            replacement = list(event.get_messages())
            # Replace records in place.  For a quoted record, update only the
            # Reply.chain entry and its display text; the Reply object itself
            # remains available to quote-aware handlers.
            for kind, outer_index, nested_index in locations:
                if kind == "top":
                    replacement[outer_index] = Plain(transcript)
                else:
                    reply = replacement[outer_index]
                    chain = list(reply.chain or [])
                    chain[nested_index] = Plain(transcript)
                    reply.chain = chain
                    quoted_text = ''.join(str(getattr(part, 'text', '') or '') for part in chain if isinstance(part, Plain)).strip()
                    if hasattr(reply, 'message_str'):
                        reply.message_str = quoted_text
                    if hasattr(reply, 'text'):
                        reply.text = quoted_text
            # Only top-level text and the newly transcribed voice are current
            # input.  Existing quoted prose remains context, not an
            # instruction from the current sender.
            message_text = ''.join(str(getattr(part, 'text', '') or '') for part in replacement if isinstance(part, Plain)).strip()
            quoted_transcripts = [transcript for kind, _, _ in locations if kind == "reply"]
            if quoted_transcripts:
                current_text = ''.join(str(getattr(part, 'text', '') or '') for part in replacement if isinstance(part, Plain)).strip()
                message_text = '\n'.join(item for item in (current_text, *quoted_transcripts) if item).strip()
            if len(message_text) > MAX_TEXT_CHARS:
                raise VoiceInputError('too_long')
            safe, unused_info = safety_check(message_text)
            if not safe:
                return await self._stop(event, 'unsafe')
            echo = MessageEventResult().message('语音识别：\n' + transcript).use_markdown(False)
            previous_send = event._has_send_oper
            previous_ids = event.get_extra('qqofficial_sent_message_ids')
            had_ids = 'qqofficial_sent_message_ids' in event.get_extra(default={})
            try:
                await asyncio.wait_for(event.send(echo), timeout=10)
                if not getattr(echo, 'qqofficial_sent_message_ids', None):
                    raise VoiceInputError('echo_failed')
            except Exception:
                return await self._stop(event, 'echo_failed', notify=False)
            finally:
                event._has_send_oper = previous_send
                if had_ids:
                    event.set_extra('qqofficial_sent_message_ids', previous_ids)
                else:
                    event.get_extra(default={}).pop('qqofficial_sent_message_ids', None)
            event.message_obj.message[:] = replacement
            event.message_str = message_text
            event.message_obj.message_str = message_text
            event.set_extra('voice_input_transcript', transcript)
            event.set_extra('voice_input_transcribed', True)
            logger.info('voice_input recognized duration_seconds=%.2f chars=%d',
                        result.get('duration_seconds', 0), len(transcript))
            return True
        except asyncio.CancelledError:
            event.stop_event()
            raise
        except VoiceInputError as error:
            return await self._stop(event, error.code)
        except Exception:
            return await self._stop(event, 'recognition_failed')

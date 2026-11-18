from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import resource
import stat
import sys
import time
import unicodedata


STATE_DIRECTORY = Path(os.environ.get('ASTRBOT_VOICE_STATE_DIRECTORY', '/var/lib/astrbot/data/voice-input'))
MODEL_DIRECTORY = Path(os.environ.get('ASTRBOT_VOICE_MODEL_DIRECTORY', '/var/lib/astrbot/data/models/sensevoice-20240717-int8'))
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_AUDIO_SECONDS = 60
SAMPLE_RATE = 16000


class RecognitionError(Exception):
    pass


class LimitedPCM(io.BytesIO):
    def write(self, payload):
        if self.tell() + len(payload) > MAX_AUDIO_SECONDS * 24000 * 2:
            raise RecognitionError('too_long')
        return super().write(payload)


def read_input(relative_file: str, temporary_root: Path) -> bytes:
    if not isinstance(relative_file, str):
        raise RecognitionError('invalid_audio')
    relative = Path(relative_file)
    if (relative.is_absolute() or len(relative.parts) != 2
            or not re.fullmatch(r'voice-[a-zA-Z0-9_-]+', relative.parts[0])
            or relative.parts[1] != 'input.audio'):
        raise RecognitionError('invalid_audio')
    candidate = temporary_root / relative
    if candidate.resolve().parent.parent != temporary_root.resolve() or candidate.parent.is_symlink():
        raise RecognitionError('invalid_audio')
    descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise RecognitionError('invalid_audio')
        if metadata.st_size > MAX_AUDIO_BYTES:
            raise RecognitionError('too_large')
        payload = source.read(MAX_AUDIO_BYTES + 1)
    if len(payload) > MAX_AUDIO_BYTES:
        raise RecognitionError('too_large')
    if not payload:
        raise RecognitionError('empty_audio')
    return payload


def audio_format(payload: bytes) -> str:
    if payload.startswith(b'RIFF') and payload[8:12] == b'WAVE':
        return 'wav'
    if payload.startswith(b'OggS'):
        return 'ogg'
    if payload.startswith(b'fLaC'):
        return 'flac'
    if payload.startswith(b'#!AMR'):
        return 'amr'
    if payload[4:8] == b'ftyp':
        return 'mov'
    if payload.startswith(b'\x1aE\xdf\xa3'):
        return 'matroska'
    if payload.startswith(b'ID3'):
        return 'mp3'
    if len(payload) >= 2 and payload[0] == 255:
        if payload[1] & 0xF6 == 0xF0:
            return 'aac'
        if payload[1] & 0xE0 == 0xE0:
            return 'mp3'
    raise RecognitionError('unsupported_audio')


def decode_audio(payload: bytes):
    import av
    import numpy

    if payload.startswith((b'\x02#!SILK_V3', b'#!SILK_V3')):
        import pysilk

        output = LimitedPCM()
        if payload.startswith(b'\x02'):
            payload = payload[1:]
        pysilk.decode(io.BytesIO(payload), output, 24000)
        decoded = output.getvalue()
        if not decoded:
            raise RecognitionError('empty_audio')
        container_source = io.BytesIO()
        import wave

        with wave.open(container_source, 'wb') as destination:
            destination.setnchannels(1)
            destination.setsampwidth(2)
            destination.setframerate(24000)
            destination.writeframes(decoded)
        payload = container_source.getvalue()
    resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
    chunks = []
    sample_count = 0

    def append_frames(frames):
        nonlocal sample_count
        for frame in frames:
            values = frame.to_ndarray().reshape(-1)
            sample_count += len(values)
            if sample_count > MAX_AUDIO_SECONDS * SAMPLE_RATE:
                raise RecognitionError('too_long')
            chunks.append(values)

    with av.open(io.BytesIO(payload), mode='r', format=audio_format(payload),
                 options={'protocol_whitelist': 'pipe', 'probesize': '1048576', 'analyzeduration': '5000000'}) as container:
        if not container.streams.audio:
            raise RecognitionError('unsupported_audio')
        stream = container.streams.audio[0]
        if stream.duration is not None and stream.time_base is not None:
            if float(stream.duration * stream.time_base) > MAX_AUDIO_SECONDS + 0.1:
                raise RecognitionError('too_long')
        for frame in container.decode(stream):
            if not 8000 <= frame.sample_rate <= 192000 or len(frame.layout.channels) > 8:
                raise RecognitionError('unsupported_audio')
            append_frames(resampler.resample(frame))
        append_frames(resampler.resample(None))
    if sample_count < SAMPLE_RATE // 5:
        raise RecognitionError('empty_audio')
    samples = numpy.concatenate(chunks).astype(numpy.float32) / 32768
    return samples


def recognize(payload: bytes, model_directory: Path = MODEL_DIRECTORY) -> dict:
    import numpy
    import sherpa_onnx

    started = time.monotonic()
    samples = decode_audio(payload)
    vad_config = sherpa_onnx.VadModelConfig()
    vad_config.sample_rate = SAMPLE_RATE
    vad_config.num_threads = 1
    vad_config.silero_vad.model = str(model_directory / 'silero_vad.onnx')
    vad_config.silero_vad.threshold = 0.5
    vad_config.silero_vad.min_speech_duration = 0.2
    vad_config.silero_vad.min_silence_duration = 0.3
    vad_config.silero_vad.max_speech_duration = MAX_AUDIO_SECONDS
    detector = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=MAX_AUDIO_SECONDS + 2)
    for offset in range(0, len(samples), vad_config.silero_vad.window_size):
        detector.accept_waveform(samples[offset:offset + vad_config.silero_vad.window_size])
    detector.flush()
    segments = []
    while not detector.empty():
        segments.append(numpy.asarray(detector.front.samples, dtype=numpy.float32).copy())
        detector.pop()
    speech_samples = sum(len(segment) for segment in segments)
    if speech_samples < SAMPLE_RATE // 5:
        raise RecognitionError('empty_audio')
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_directory / 'model.int8.onnx'), tokens=str(model_directory / 'tokens.txt'),
        num_threads=1, language='zh', use_itn=True,
    )
    stream = recognizer.create_stream()
    stream.accept_waveform(SAMPLE_RATE, samples)
    recognizer.decode_stream(stream)
    text = re.sub(r'<\|[^|]+\|>', '', stream.result.text)
    text = ''.join(character for character in text if unicodedata.category(character)[0] != 'C' or character in '\n\t').strip()
    if not text or not any(character.isalnum() for character in text):
        raise RecognitionError('empty_audio')
    if len(text) > 3000:
        raise RecognitionError('too_long')
    return {'ok': True, 'text': text, 'duration_seconds': round(len(samples) / SAMPLE_RATE, 3),
            'speech_seconds': round(speech_samples / SAMPLE_RATE, 3),
            'latency_ms': round((time.monotonic() - started) * 1000),
            'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}


def main():
    os.umask(0o077)
    try:
        line = sys.stdin.buffer.readline(4097)
        if len(line) > 4096 or not line.endswith(b'\n'):
            raise RecognitionError('invalid_audio')
        request = json.loads(line)
        if not isinstance(request, dict) or set(request) != {'file'}:
            raise RecognitionError('invalid_audio')
        payload = read_input(request['file'], STATE_DIRECTORY / 'tmp')
        result = recognize(payload)
    except RecognitionError as error:
        result = {'ok': False, 'code': str(error)}
    except Exception as error:
        print('voice_input worker failed type=' + type(error).__name__, file=sys.stderr, flush=True)
        result = {'ok': False, 'code': 'recognition_failed'}
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

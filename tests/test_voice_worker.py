"""Offline voice worker boundaries; decoder/model checks run when installed."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('voice_worker_under_test', ROOT / 'services/voice_worker.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
DECODER = importlib.util.find_spec('av') is not None and importlib.util.find_spec('numpy') is not None


def wav(seconds=1, rate=16000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(rate)
        destination.writeframes(bytes(int(rate * seconds) * 2))
    return output.getvalue()


class WorkerTests(unittest.TestCase):
    def test_input_is_exact_bounded_regular_file_under_temporary_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / 'voice-synthetic' / 'input.audio'
            audio.parent.mkdir()
            audio.write_bytes(b'current synthetic audio')
            self.assertEqual(worker.read_input('voice-synthetic/input.audio', root), b'current synthetic audio')
            for name in ('../private', '/tmp/input.audio', 'voice-synthetic/other.audio', 'not-voice/input.audio', 'voice-a/b/input.audio'):
                with self.subTest(name=name), self.assertRaises(worker.RecognitionError):
                    worker.read_input(name, root)
            audio.write_bytes(b'')
            with self.assertRaisesRegex(worker.RecognitionError, 'empty_audio'):
                worker.read_input('voice-synthetic/input.audio', root)
            audio.write_bytes(b'123456')
            with patch.object(worker, 'MAX_AUDIO_BYTES', 5), self.assertRaisesRegex(worker.RecognitionError, 'too_large'):
                worker.read_input('voice-synthetic/input.audio', root)

    def test_input_symlink_and_symlink_parent_cannot_read_another_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'synthetic-private'
            private.write_bytes(b'not audio')
            audio = root / 'voice-synthetic' / 'input.audio'
            audio.parent.mkdir()
            audio.symlink_to(private)
            with self.assertRaises((OSError, worker.RecognitionError)):
                worker.read_input('voice-synthetic/input.audio', root)
            audio.unlink()
            audio.parent.rmdir()
            audio.parent.symlink_to(root, target_is_directory=True)
            with self.assertRaises(worker.RecognitionError):
                worker.read_input('voice-synthetic/input.audio', root)

    def test_signature_detection_rejects_playlists_and_unknown_content(self):
        for payload, expected in ((wav(), 'wav'), (b'OggSsynthetic', 'ogg'), (b'fLaCsynthetic', 'flac'),
                                  (b'#!AMRsynthetic', 'amr'), (b'ID3synthetic', 'mp3'),
                                  (b'\xff\xf1synthetic', 'aac'), (b'\xff\xfbsynthetic', 'mp3')):
            self.assertEqual(worker.audio_format(payload), expected)
        for payload in (b'#EXTM3U\nhttps://example.com/audio', b'<html>not audio</html>', b'', b'unknown'):
            with self.assertRaisesRegex(worker.RecognitionError, 'unsupported_audio'):
                worker.audio_format(payload)

    def test_silk_decoder_output_cannot_grow_past_the_duration_budget(self):
        output = worker.LimitedPCM()
        with patch.object(worker, 'MAX_AUDIO_SECONDS', 1):
            output.write(bytes(48000))
            with self.assertRaisesRegex(worker.RecognitionError, 'too_long'):
                output.write(b'\0')
        self.assertEqual(output.tell(), 48000)

    def test_invalid_stdio_request_returns_no_path_or_transcript(self):
        for data in ('{"file":"../private"}\n', '{"file":"voice-synthetic/input.audio","other":true}\n',
                     'not json\n', '{"file":"voice-synthetic/input.audio"}'):
            result = subprocess.run([sys.executable, str(ROOT / 'services/voice_worker.py')], input=data,
                                    capture_output=True, text=True, check=True, timeout=10)
            response = json.loads(result.stdout)
            self.assertFalse(response['ok'])
            self.assertNotIn('text', response)
            self.assertNotIn('../private', result.stdout + result.stderr)

    @unittest.skipUnless(DECODER, 'optional voice decoder environment is not installed')
    def test_real_decoder_normalizes_sample_rate_and_rejects_short_or_long_audio(self):
        samples = worker.decode_audio(wav(1, rate=24000))
        self.assertEqual(len(samples), 16000)
        self.assertFalse(samples.any())
        for payload, code in ((wav(0.1), 'empty_audio'), (wav(61), 'too_long')):
            with self.subTest(code=code), self.assertRaisesRegex(worker.RecognitionError, code):
                worker.decode_audio(payload)

    @unittest.skipUnless(DECODER and importlib.util.find_spec('sherpa_onnx') is not None
                         and (worker.MODEL_DIRECTORY / 'silero_vad.onnx').is_file(), 'optional offline VAD model is not installed')
    def test_real_vad_rejects_silence_without_inventing_a_command(self):
        with self.assertRaisesRegex(worker.RecognitionError, 'empty_audio'):
            worker.recognize(wav(2))


if __name__ == '__main__':
    unittest.main()

"""Verify cumulative patch helpers and, optionally, the pinned upstream wheel."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('voice_patch_installer', ROOT / 'scripts/apply_patches.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)
MANIFEST = json.loads((ROOT / 'patches/manifest.json').read_text())
WHEEL = Path(os.environ['ASTRBOT_WHEEL']) if os.environ.get('ASTRBOT_WHEEL') else None
MIRRORS = {
    'astrbot/core/pipeline/preprocess_stage/voice_input.py': 'patches/astrbot-voice/voice_input.py',
    'astrbot/core/platform/sources/qqofficial/text_mentions.py': 'patches/astrbot-qq-mentions/text_mentions.py',
    'astrbot/core/utils/public_image_download.py': 'patches/astrbot-public-images/public_image_download.py',
    'astrbot/core/platform/sources/qqofficial/markdown_images.py': 'patches/astrbot-qq-markdown/markdown_images.py',
}


class VoicePatchTests(unittest.TestCase):
    def test_all_changed_framework_sources_are_pinned_and_helpers_match(self):
        entries = {entry['path']: entry for entry in MANIFEST['files']}
        expected = {
            'astrbot/core/pipeline/preprocess_stage/stage.py',
            'astrbot/core/pipeline/preprocess_stage/voice_input.py',
            'astrbot/core/pipeline/waking_check/stage.py',
            'astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py',
            'astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py',
            'astrbot/core/platform/sources/qqofficial/text_mentions.py',
            'astrbot/core/platform/sources/qqofficial/markdown_images.py',
            'astrbot/core/provider/sources/openai_source.py',
            'astrbot/core/tools/web_search_tools.py',
            'astrbot/core/utils/media_utils.py',
            'astrbot/core/utils/public_image_download.py',
        }
        self.assertEqual(set(entries), expected)
        lines = (ROOT / 'patches/astrbot-4.29.3-customizations.patch').read_text().splitlines(keepends=True)
        for path, mirror in MIRRORS.items():
            start = lines.index('+++ b/' + path + '\n') + 1
            additions = []
            for line in lines[start:]:
                if line.startswith('--- '): break
                if line.startswith('+'): additions.append(line[1:])
            source = ''.join(additions).encode()
            self.assertIsNone(entries[path]['original_sha256'])
            self.assertEqual(source, (ROOT / mirror).read_bytes())
            self.assertEqual(hashlib.sha256(source).hexdigest(), entries[path]['patched_sha256'])
            ast.parse(source.decode())

    def test_optional_service_keeps_one_shot_offline_resource_boundaries(self):
        service = (ROOT / 'deploy/systemd/voice-input@.service').read_text()
        for option in ('User=astrbot', 'PrivateNetwork=yes', 'NoNewPrivileges=yes', 'ProtectSystem=strict',
                       'MemoryMax=600M', 'RuntimeMaxSec=65', 'StandardInput=socket', 'RestrictAddressFamilies=AF_UNIX'):
            self.assertIn(option, service)
        socket = (ROOT / 'deploy/systemd/voice-input.socket').read_text()
        for option in ('SocketMode=0600', 'Accept=yes', 'MaxConnections=1'):
            self.assertIn(option, socket)
        self.assertFalse(json.loads((ROOT / 'deploy/examples/voice-config.json.example').read_text())['enabled'])

    @unittest.skipUnless(WHEEL and WHEEL.is_file(), 'set ASTRBOT_WHEEL to the original AstrBot 4.29.3 wheel')
    def test_dry_run_apply_hashes_and_idempotency_against_original_wheel(self):
        with tempfile.TemporaryDirectory(prefix='voice-framework-patch-') as directory:
            site = Path(directory)
            with zipfile.ZipFile(WHEEL) as wheel:
                for entry in MANIFEST['files']:
                    path = site / entry['path']
                    if entry['original_sha256'] is None:
                        self.assertNotIn(entry['path'], wheel.namelist())
                    else:
                        raw = wheel.read(entry['path'])
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['original_sha256'])
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(raw)
            self.assertEqual(installer.source_state(site, MANIFEST['files']), 'original')
            self.assertIn('no changes', installer.apply_patches(site, check=True))
            self.assertEqual(installer.source_state(site, MANIFEST['files']), 'original')
            self.assertIn('verified', installer.apply_patches(site))
            self.assertEqual(installer.source_state(site, MANIFEST['files']), 'patched')
            for entry in MANIFEST['files']:
                source = (site / entry['path']).read_bytes()
                self.assertEqual(hashlib.sha256(source).hexdigest(), entry['patched_sha256'])
                ast.parse(source.decode())
            self.assertIn('already installed', installer.apply_patches(site))


if __name__ == '__main__':
    unittest.main()

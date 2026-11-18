"""Complete baseline rendering and native text plans use only synthetic records."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock

import test_watcher as harness

ROOT = Path(__file__).parent
PACKAGE = '_xianyu_formatting_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(PACKAGE + '.formatting', ROOT / 'formatting.py')
formatting = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = formatting
spec.loader.exec_module(formatting)
LINK = re.compile(r'\]\(https://www\.goofish\.com/item\?id=([0-9]+)\)')
OTHER_SCOPE = 'synthetic_bot:GroupMessage:display-group'
WATCH_ID = 'a' * 16


def record(long_title=False):
    ids = [str(8000 + index) for index in range(1, 48)]
    snapshots = {}
    for index, item_id in enumerate(ids[:-1], 1):
        snapshots[item_id] = {'item_id': item_id, 'title': ('合成' * 150 if long_title else '合成商品 ' + str(index)),
            'price': '10.00' if index <= 40 else '100', 'url': 'https://untrusted.invalid/not-an-item',
            'source': 'recovered_current_query', 'observed_at': 1000, 'recovered_at': 1000}
    return {'id': WATCH_ID, 'umo': harness.SCOPE, 'sender': 'original-owner', 'owner': 'synthetic-owner-key',
            'state': 'active', 'interval_minutes': 45, 'max_price': 100, 'baseline_ids': ids,
            'baseline_snapshots': snapshots, 'baseline_at': None, 'baseline_legacy': True,
            'next_run_at': 2000, 'seen_ids': {item_id: 100 for item_id in ids}, 'pending_alerts': []}


class FormatterTests(unittest.TestCase):
    def test_product_name_is_safe_markdown_and_link_target_is_canonical(self):
        row = {'item_id': '123', 'title': '合成 [套装](https://untrusted.invalid/) <qqbot-at-user id="x" />\n*_',
               'price': '12.50', 'url': 'https://untrusted.invalid/override'}
        line = formatting.product_markdown(row, 1)
        self.assertEqual(LINK.findall(line), ['123'])
        self.assertIn(r'\[套装\]\(', line)
        self.assertIn('&lt;qqbot-at-user', line)
        self.assertNotIn('<qqbot-at-user', line)
        self.assertNotIn('\n', line)
        self.assertNotIn('untrusted.invalid/override', line)
        for value in ('../123', 'x', '', '123?redirect=bad'):
            with self.assertRaises(ValueError):
                formatting.item_url(value)

    def test_all_forty_matching_items_have_links_in_four_complete_pages(self):
        rendered = formatting.baseline_pages(record())
        self.assertEqual(rendered['total_items'], 40)
        self.assertEqual(rendered['baseline_total'], 47)
        self.assertEqual(rendered['known_matching_count'], 40)
        self.assertEqual(rendered['details_unavailable_count'], 1)
        self.assertEqual([page['item_count'] for page in rendered['pages']], [10, 10, 10, 10])
        text = '\n'.join(page['text'] for page in rendered['pages'])
        self.assertEqual(LINK.findall(text), [str(8000 + i) for i in range(1, 41)])
        self.assertEqual(re.findall(r'(?m)^([0-9]+)\. \[', text), [str(i) for i in range(1, 41)])
        for page in rendered['pages']:
            self.assertIn('每 45 分钟', page['text'])
            self.assertIn('恢复时数据', page['text'])
            self.assertIn('无法还原首次名称和价格', page['text'])
            self.assertIn('详情未恢复 1 件', page['text'])
            self.assertLessEqual(len(page['text'].encode()), 3500)
        self.assertEqual(text.count(' · 补查'), 40)

    def test_long_titles_split_more_pages_without_truncating_a_single_entry(self):
        rendered = formatting.baseline_pages(record(long_title=True))
        self.assertGreater(len(rendered['pages']), 4)
        text = '\n'.join(page['text'] for page in rendered['pages'])
        self.assertEqual(len(LINK.findall(text)), 40)
        self.assertEqual(text.count('合成' * 150), 40)
        for page in rendered['pages']:
            self.assertLessEqual(len(page['text'].encode()), 3500)
            self.assertLessEqual(page['item_count'], 10)

    def test_all_baseline_mode_labels_missing_details_without_guessing_a_name(self):
        rendered = formatting.baseline_pages(record(), matching_only=False)
        text = '\n'.join(page['text'] for page in rendered['pages'])
        self.assertEqual(rendered['total_items'], 47)
        self.assertEqual(LINK.findall(text), [str(8000 + i) for i in range(1, 48)])
        self.assertIn('详情未恢复（商品 8047）', text)
        self.assertIn('价格未知', text)

    def test_no_budget_retains_existing_baseline_matching_semantics(self):
        value = record()
        value['max_price'] = None
        value['baseline_snapshots']['8001']['price'] = '价格面议'
        rendered = formatting.baseline_pages(value)
        self.assertEqual(rendered['known_matching_count'], 46)
        self.assertEqual(rendered['total_items'], 46)
        self.assertIn('未设价格上限', rendered['pages'][0]['text'])
        self.assertIn('价格面议', rendered['pages'][0]['text'])

    def test_delivery_plan_is_stable_and_bound_to_the_current_scope_and_actor(self):
        pages = formatting.baseline_pages(record())['pages']
        first = formatting.delivery_plan('show_xianyu_baseline', pages, scope='scope', sender='actor', watch_id=WATCH_ID)
        self.assertEqual(first, formatting.delivery_plan('show_xianyu_baseline', pages, scope='scope', sender='actor', watch_id=WATCH_ID))
        other = formatting.delivery_plan('show_xianyu_baseline', pages, scope='scope', sender='other', watch_id=WATCH_ID)
        self.assertNotEqual(first['plan_id'], other['plan_id'])
        self.assertEqual(first['chunk_count'], 4)
        self.assertEqual(first['total_items'], 40)


class DisplayTools(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.context = harness.Context()
        self.bridge = harness.Main(self.context, {'allowed_umos': [harness.SCOPE, OTHER_SCOPE],
            'state_file': str(Path(self.tmp.name) / 'watches.json'), 'send_product_images': False})
        self.bridge.records[WATCH_ID] = record()
        self.source = harness.Event('current-actor', OTHER_SCOPE)
        self.source.sent = []
        async def send(chain):
            self.source.sent.append(chain)
            return True
        self.source.send = send

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.tmp.cleanup()

    async def test_shared_baseline_show_returns_short_plan_and_preserves_all_state(self):
        before = copy.deepcopy(self.bridge.records)
        result = await self.bridge._show_baseline(self.source, WATCH_ID)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(self.source.sent), 4)
        self.assertEqual(result['delivery_plan']['total_items'], 40)
        self.assertEqual(result['delivery_plan']['tool'], 'show_xianyu_baseline')
        self.assertLess(len(json.dumps(result, ensure_ascii=False)), 1500)
        self.assertNotIn('合成商品', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('items', result)
        self.assertEqual(self.bridge.records, before)
        self.assertFalse(self.bridge.file.exists())
        for index, chain in enumerate(self.source.sent):
            self.assertTrue(chain.use_markdown_)
            self.assertFalse(chain.use_t2i_)
            self.assertTrue(all(isinstance(part, harness.Plain) for part in chain.chain))
            metadata = chain.xianyu_delivery
            self.assertEqual(metadata['plan_id'], result['delivery_plan']['plan_id'])
            self.assertEqual(metadata['chunk_index'], index)
            self.assertEqual(metadata['item_count'], 10)
            self.assertEqual(metadata['total_items'], 40)

    async def test_denied_caller_or_revoked_source_does_not_emit_any_page(self):
        self.bridge.allowed_umos = {OTHER_SCOPE}
        result = await self.bridge._show_baseline(self.source, WATCH_ID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.source.sent, [])
        self.bridge.allowed_umos = {harness.SCOPE}
        result = await self.bridge._show_baseline(self.source, WATCH_ID)
        self.assertFalse(result['ok'])
        self.assertEqual(self.source.sent, [])

    async def test_native_send_failure_does_not_claim_complete_delivery(self):
        async def send(chain):
            self.source.sent.append(chain)
            return len(self.source.sent) < 2
        self.source.send = send
        result = await self.bridge._show_baseline(self.source, WATCH_ID)
        self.assertFalse(result['ok'])
        self.assertEqual(result['prepared_chunks'], 1)
        self.assertNotIn('complete', result)
        self.assertEqual(result['delivery_plan']['chunk_count'], 4)

    async def test_search_prepares_native_links_and_keeps_structured_result_compatibility(self):
        client = harness.FakeClient()
        client.rows = [dict(harness.row(1), title='合成查询商品 [一]'), harness.row(2)]
        client.download_image = Mock(side_effect=AssertionError('product images disabled'))
        self.bridge.client = lambda: client
        result = await self.bridge._search_reply(self.source, '合成测试商品', 10)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['delivery_plan']['tool'], 'xianyu_search')
        self.assertEqual(result['delivery_plan']['total_items'], 2)
        self.assertEqual(result['image_count'], 0)
        self.assertEqual(len(result['results']), 2)
        self.assertTrue(all('display_markdown' in item and 'image_url' not in item for item in result['results']))
        self.assertEqual(LINK.findall(self.source.sent[0].chain[0].text), ['1', '2'])
        self.assertTrue(self.source.sent[0].use_markdown_)
        client.download_image.assert_not_called()

    async def test_pending_notification_uses_the_same_named_markdown_link(self):
        self.bridge.records = {}
        client = harness.FakeClient()
        self.bridge.client = lambda: client
        source = harness.Event('original-owner')
        client.rows = [harness.row(1)]
        created = await self.bridge._create(source, ['合成测试商品'])
        client.rows = [harness.row(1), dict(harness.row(2), title='合成新商品 [二]')]
        await self.bridge._run(created['watch']['id'])
        chain = self.context.sent[0][1]
        self.assertTrue(chain.use_markdown_)
        self.assertFalse(chain.use_t2i_)
        self.assertIn('[合成新商品 \\[二\\]](https://www.goofish.com/item?id=2)', chain.chain[0].text)
        self.assertEqual(LINK.findall(chain.chain[0].text), ['2'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

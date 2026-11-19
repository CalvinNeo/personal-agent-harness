"""Independent synthetic tests for shared watches, price limits and snapshots."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import test_watcher as harness


spec = importlib.util.spec_from_file_location("monitor_condition_helpers", Path(__file__).with_name("watcher.py"))
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)
SECOND_SCOPE = "synthetic_bot:GroupMessage:second-test-group"
DENIED_SCOPE = "synthetic_bot:GroupMessage:unapproved-test-group"


def product(identifier, price="12.50", title="合成测试商品"):
    return {"item_id": str(identifier), "title": title, "price": price,
            "url": "https://h5.m.goofish.com/item?id=" + str(identifier),
            "image_url": "https://img.alicdn.com/synthetic-fixture.jpg"}


class Client(harness.FakeClient):
    def __init__(self):
        super().__init__()
        self.image_calls = []
        self.search_calls = []

    def search(self, keyword, limit):
        self.search_calls.append((keyword, limit))
        return super().search(keyword, limit)

    def download_image(self, url):
        self.image_calls.append(url)
        return super().download_image(url)


class PriceContracts(unittest.TestCase):
    def setUp(self):
        self.numeric_price = watcher.numeric_price
        self.matches_price = watcher.matches_price

    def test_price_limit_is_strict_and_cannot_guess_unknown_amounts(self):
        self.assertTrue(self.matches_price(product(1, "9.99"), 10))
        self.assertFalse(self.matches_price(product(1, "10"), 10))
        self.assertFalse(self.matches_price(product(1, "10.01"), 10))
        for value in ("面议", "价格面议", "", "9-11", "9~11", "9至11", "定金9", "运费9", "9+5运费", "9起"):
            with self.subTest(price=value):
                self.assertIsNone(self.numeric_price(value))
                self.assertFalse(self.matches_price(product(1, value), 10))
        for title in ("合成商品定金", "合成商品运费", "合成商品预付款"):
            with self.subTest(title=title):
                self.assertFalse(self.matches_price(product(1, "5", title), 10))

    def test_canonical_currency_prices_parse_without_changing_amounts(self):
        for value in ("12.50", "¥12.50", "￥12.50", "12.50元"):
            with self.subTest(price=value):
                self.assertEqual(self.numeric_price(value), 12.5)
        for value in (True, False, float("nan"), float("inf"), -1):
            with self.subTest(price=repr(value)):
                self.assertIsNone(self.numeric_price(value))


class MonitorConditions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="monitor-conditions-")
        self.file = Path(self.tmp.name) / "watches.json"
        self.context = harness.Context()
        self.client = Client()
        self.owner = harness.Event("synthetic-owner")
        self.peer = harness.Event("synthetic-peer", SECOND_SCOPE)
        self.config = {"state_file": str(self.file), "allowed_umos": [harness.SCOPE, SECOND_SCOPE],
                       "allowed_senders": [], "send_product_images": False}
        self.bridge = self.new()

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.tmp.cleanup()

    def new(self):
        bridge = harness.Main(self.context, self.config)
        bridge.client = lambda: self.client
        return bridge

    async def create(self, rows=None, **conditions):
        self.client.rows = copy.deepcopy(rows if rows is not None else [product(1)])
        response = await self.bridge._create(self.owner, ["合成测试相机"], **conditions)
        self.assertTrue(response["ok"], response)
        return response["watch"]["id"]

    async def test_shared_member_can_list_and_manage_without_changing_delivery_route(self):
        watch_id = await self.create()
        listed = await self.bridge._list(self.peer)
        self.assertTrue(listed["ok"], listed)
        self.assertIn(watch_id, [item["id"] for item in listed["watches"]])
        for action in ("pause", "resume"):
            response = await self.bridge._manage(self.peer, watch_id, action)
            self.assertTrue(response["ok"], response)
        self.assertEqual(self.bridge.records[watch_id]["umo"], harness.SCOPE)
        self.assertEqual(self.bridge.records[watch_id]["sender"], self.owner.sender)
        self.client.rows = [product(1), product(2)]
        await self.bridge._run(watch_id)
        self.assertEqual(len(self.context.sent), 1)
        self.assertEqual(self.context.sent[0][0], harness.SCOPE)

    async def test_shared_updates_validate_parameters_and_persist_after_restart(self):
        watch_id = await self.create()
        response = await self.bridge._manage(self.peer, watch_id, "update", max_price=20,
                                              interval_minutes=45, max_results=6)
        self.assertTrue(response["ok"], response)
        self.bridge = self.new()
        self.assertTrue(self.bridge._storage_ok)
        record = self.bridge.records[watch_id]
        self.assertEqual(record["max_price"], 20)
        self.assertEqual(record["interval_minutes"], 45)
        self.assertEqual(record["max_results"], 6)
        self.assertEqual(record["umo"], harness.SCOPE)
        omitted = await self.bridge._manage(self.peer, watch_id, "update", interval_minutes=60)
        self.assertTrue(omitted["ok"], omitted)
        self.assertEqual(self.bridge.records[watch_id]["max_price"], 20)
        cleared = await self.bridge._manage(self.peer, watch_id, "update", max_price=None)
        self.assertTrue(cleared["ok"], cleared)
        self.assertIsNone(self.bridge.records[watch_id]["max_price"])

    async def test_invalid_price_conditions_do_not_create_or_mutate_a_watch(self):
        for value in (True, False, 0, -1, float("nan"), float("inf"), "10"):
            with self.subTest(max_price=repr(value)):
                response = await self.bridge._create(self.owner, ["合成测试相机"], max_price=value)
                self.assertFalse(response["ok"], response)
        self.assertEqual(self.bridge.records, {})
        watch_id = await self.create(max_price=20)
        before = copy.deepcopy(self.bridge.records[watch_id])
        for value in (True, -1, float("nan"), float("inf")):
            response = await self.bridge._manage(self.peer, watch_id, "update", max_price=value)
            self.assertFalse(response["ok"], response)
            self.assertEqual(self.bridge.records[watch_id], before)

    async def test_price_filter_sends_only_strictly_matching_new_products_without_images(self):
        watch_id = await self.create(rows=[product(1, "1")], max_price=10)
        self.client.rows = [product(1, "1"), product(2, "9.99"), product(3, "10"),
                            product(4, "价格面议"), product(5, "定金5"), product(6, "5+运费5")]
        await self.bridge._run(watch_id)
        await self.bridge._run(watch_id)
        self.assertEqual(len(self.context.sent), 1)
        text = "\n".join(part.text for part in self.context.sent[0][1].chain if isinstance(part, harness.Plain))
        self.assertIn("item?id=2", text)
        self.assertIn("9.99", text)
        self.assertNotIn("图片暂不可用", text)
        self.assertTrue(all(isinstance(part, harness.Plain) for part in self.context.sent[0][1].chain))
        self.assertEqual(self.client.image_calls, [])
        # An initially nonmatching listing must not become a newly published
        # product merely because its price or missing price later changes.
        self.client.rows = [product(1, "1"), product(2, "9.99"), product(3, "5"), product(4, "5")]
        await self.bridge._run(watch_id)
        self.assertEqual(len(self.context.sent), 1)

    async def test_baseline_keeps_all_original_metadata_time_and_price_matches_after_restart(self):
        initial = [product(1, "9", "合成最初标题"), product(2, "20"), product(3, "价格面议")]
        watch_id = await self.create(initial, max_price=10)
        response = await self.bridge._read(self.peer, watch_id, limit=20)
        self.assertTrue(response["ok"], response)
        baseline = copy.deepcopy(response["baseline"])
        self.assertEqual(baseline["total"], 3)
        self.assertFalse(baseline["legacy"])
        self.assertTrue(baseline["matching_complete"])
        self.assertEqual(baseline["known_matching_count"], 1)
        self.assertEqual(baseline["details_unavailable_count"], 0)
        self.assertIsNotNone(baseline["snapshot_at"])
        items = {item["item_id"]: item for item in baseline["items"]}
        self.assertEqual(set(items), {"1", "2", "3"})
        self.assertTrue(items["1"]["matches_price"])
        self.assertFalse(items["2"]["matches_price"])
        self.assertFalse(items["3"]["matches_price"])
        for item in items.values():
            self.assertTrue(item["details_available"])
            self.assertEqual(item["source"], "initial_query")
            self.assertIsNotNone(item["observed_at"])
            self.assertNotIn("image_url", item)
        self.client.rows = [product(1, "100", "合成后来的标题"), product(4, "2")]
        await self.bridge._run(watch_id)
        self.assertIn("4", self.bridge.records[watch_id]["seen_ids"])
        self.bridge = self.new()
        self.assertTrue(self.bridge._storage_ok)
        restored = await self.bridge._read(self.peer, watch_id, limit=20)
        self.assertEqual(restored["baseline"], baseline)
        self.assertEqual(self.client.image_calls, [])

    async def test_baseline_pagination_does_not_requery_or_change_snapshot(self):
        watch_id = await self.create([product(1), product(2), product(3)])
        before_calls = len(self.client.search_calls)
        first = await self.bridge._read(self.peer, watch_id, offset=0, limit=2)
        second = await self.bridge._read(self.peer, watch_id, offset=2, limit=2)
        self.assertTrue(first["ok"] and second["ok"], (first, second))
        self.assertTrue(first["baseline"]["has_more"])
        self.assertFalse(second["baseline"]["has_more"])
        ids = [item["item_id"] for item in first["baseline"]["items"] + second["baseline"]["items"]]
        self.assertEqual(set(ids), {"1", "2", "3"})
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(self.client.search_calls), before_calls)
        self.assertEqual(self.context.sent, [])

    async def test_legacy_ids_report_missing_details_and_refresh_only_the_same_ids_without_delivery(self):
        watch_id = await self.create([product(1), product(2)])
        data = json.loads(self.file.read_text())
        record = data["watches"][0]
        for key in list(record):
            if key.startswith("baseline") and key != "baseline_ready":
                record.pop(key)
        self.file.write_text(json.dumps(data))
        self.bridge = self.new()
        self.assertTrue(self.bridge._storage_ok)
        before = copy.deepcopy(self.bridge.records[watch_id])
        response = await self.bridge._read(self.peer, watch_id)
        self.assertTrue(response["ok"], response)
        baseline = response["baseline"]
        self.assertTrue(baseline["legacy"])
        self.assertIsNone(baseline["snapshot_at"])
        self.assertEqual(baseline["details_unavailable_count"], 2)
        self.assertFalse(baseline["matching_complete"])
        self.assertEqual({item["item_id"] for item in baseline["items"]}, {"1", "2"})
        for item in baseline["items"]:
            self.assertFalse(item["details_available"])
            self.assertEqual(item["source"], "legacy_ids_only")
            self.assertIsNone(item["matches_price"])
            self.assertNotIn("title", item)
        self.client.rows = [product(1, "8", "合成当前恢复标题"), product(99, "7")]
        refreshed = await self.bridge._read(self.peer, watch_id, refresh_metadata=True)
        self.assertTrue(refreshed["ok"], refreshed)
        self.assertEqual(refreshed["metadata_refresh"]["matched_count"], 1)
        self.assertEqual(refreshed["metadata_refresh"]["remaining_unavailable_count"], 1)
        items = {item["item_id"]: item for item in refreshed["baseline"]["items"]}
        self.assertEqual(set(items), {"1", "2"})
        self.assertEqual(items["1"]["title"], "合成当前恢复标题")
        self.assertEqual(items["1"]["source"], "recovered_current_query")
        self.assertIsNotNone(items["1"]["recovered_at"])
        for field in ("seen_ids", "pending_alerts", "next_run_at", "last_run"):
            self.assertEqual(self.bridge.records[watch_id].get(field), before.get(field), field)
        self.assertEqual(self.context.sent, [])
        self.assertEqual(self.client.image_calls, [])
        self.bridge = self.new()
        restored = await self.bridge._read(self.peer, watch_id)
        self.assertEqual(restored["baseline"], refreshed["baseline"])
        await self.bridge._run(watch_id)
        self.assertEqual(len(self.context.sent), 1)
        self.assertIn("item?id=99", self.context.sent[0][1].chain[0].text)

    async def test_new_price_condition_is_rechecked_before_replaying_an_old_outbox(self):
        watch_id = await self.create([product(1)])
        self.client.rows = [product(1), product(2, "100")]
        self.context.reject = True
        await self.bridge._run(watch_id)
        self.assertTrue(self.bridge.records[watch_id]["pending_alerts"])
        response = await self.bridge._manage(self.peer, watch_id, "update", max_price=10)
        self.assertTrue(response["ok"], response)
        self.context.sent.clear()
        self.context.reject = False
        self.bridge.records[watch_id]["next_delivery_at"] = 0
        await self.bridge._tick()
        self.assertEqual(self.context.sent, [])
        self.assertEqual(self.bridge.records[watch_id]["pending_alerts"], [])
        self.assertIn("2", self.bridge.records[watch_id]["seen_ids"])

    async def test_unauthorized_actor_and_revoked_source_cannot_read_or_manage_shared_watch(self):
        watch_id = await self.create()
        denied = harness.Event("synthetic-stranger", DENIED_SCOPE)
        self.assertFalse((await self.bridge._list(denied))["ok"])
        self.assertFalse((await self.bridge._read(denied, watch_id))["ok"])
        self.assertFalse((await self.bridge._manage(denied, watch_id, "cancel"))["ok"])
        self.bridge.allowed_senders = {self.owner.sender}
        self.assertFalse((await self.bridge._list(self.peer))["ok"])
        self.assertFalse((await self.bridge._read(self.peer, watch_id))["ok"])
        self.assertFalse((await self.bridge._manage(self.peer, watch_id, "cancel"))["ok"])
        self.bridge.allowed_senders = set()
        self.bridge.allowed_umos.remove(harness.SCOPE)
        listed = await self.bridge._list(self.peer)
        self.assertTrue(listed["ok"], listed)
        self.assertNotIn(watch_id, [item["id"] for item in listed["watches"]])
        self.assertFalse((await self.bridge._read(self.peer, watch_id))["ok"])
        self.assertFalse((await self.bridge._manage(self.peer, watch_id, "cancel"))["ok"])
        self.client.rows = [product(2)]
        await self.bridge._run(watch_id)
        self.assertEqual(self.context.sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Brand evidence must survive search, legacy baselines and queued alerts."""
import unittest
from unittest.mock import AsyncMock

import test_monitor_conditions as fixture


class RelevanceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.MonitorConditions.asyncSetUp
    asyncTearDown = fixture.MonitorConditions.asyncTearDown
    new = fixture.MonitorConditions.new
    create = fixture.MonitorConditions.create
    async def test_legacy_shared_latin_brand_filters_baseline_and_alerts(self):
        self.client.rows = [fixture.product(1, title="Otherbrand 游戏垫"),
                            fixture.product(2, title="NOVABABY 游戏垫")]
        result = await self.bridge._create(self.owner, ["novababy 游戏垫", "novababy 健身架"])
        self.assertTrue(result["ok"], result)
        wid = result["watch"]["id"]
        record = self.bridge.records[wid]
        view = fixture.watcher.baseline_view(record)
        self.assertEqual(view["total"], 2)
        self.assertEqual(view["known_matching_count"], 1)
        self.assertEqual(view["relevance_excluded_count"], 1)
        self.client.rows += [fixture.product(3, title="Otherbrand 早教垫"),
                             fixture.product(4, title="novababy 健身架")]
        await self.bridge._run(wid)
        self.assertEqual(len(self.context.sent), 1)
        self.assertIn("novababy", self.context.sent[0][1].chain[0].text)
        self.assertNotIn("Otherbrand", self.context.sent[0][1].chain[0].text)

    async def test_explicit_alias_update_removes_queued_unrelated_and_survives_restart(self):
        self.client.rows = [fixture.product(1, title="游戏垫")]
        created = await self.bridge._create(self.owner, ["游戏垫"])
        self.assertTrue(created["ok"], created)
        wid = created["watch"]["id"]
        self.context.reject = True
        self.client.rows = [fixture.product(10, title="Otherbrand 垫子"),
                            fixture.product(11, title="诺娃宝 游戏垫")]
        await self.bridge._run(wid)
        result = await self.bridge._manage(self.owner, wid, "update", required_terms=["NovaBaby", "诺娃宝"], product_terms=["游戏垫"])
        self.assertTrue(result["ok"], result)
        record = self.bridge.records[wid]
        self.assertEqual([a["item"]["item_id"] for a in record["pending_alerts"]], ["11"])
        self.assertIn("10", record["seen_ids"])
        self.bridge = self.new()
        self.assertEqual(self.bridge.records[wid]["required_terms"], ["novababy", "诺娃宝"])
        self.assertEqual(self.bridge.records[wid]["product_terms"], ["游戏垫"])

    async def test_instant_search_excludes_recommendations_and_returns_only_verified_titles(self):
        self.client.rows = [fixture.product(21, title="Otherbrand 垫子"),
                            fixture.product(22, title="诺娃宝 游戏垫")]
        self.owner.get_messages = lambda: []
        self.owner.send = AsyncMock(return_value=True)
        result = await self.bridge._search_reply(self.owner, "novababy 游戏垫", required_terms=["novababy", "诺娃宝"])
        self.assertTrue(result["ok"], result)
        self.assertEqual([row["item_id"] for row in result["results"]], ["22"])
        self.assertEqual(result["relevance_excluded_count"], 1)
        self.assertEqual(result["product_terms"], ["游戏垫"])
        self.assertNotIn("Otherbrand", str(self.owner.send.call_args))

    async def test_concrete_product_terms_reject_same_brand_accessories(self):
        rows = [fixture.product(30, title="infantino 毛毛虫沙发抱枕"),
                fixture.product(31, title="Infantino 婴儿健身架")]
        result = await self.bridge._create(self.owner,
                                           ["infantino 早教垫", "infantino 健身架"],
                                           required_terms=["infantino", "婴蒂诺"])
        self.assertTrue(result["ok"], result)
        record = self.bridge.records[result["watch"]["id"]]
        self.assertNotIn("product_terms", record)
        self.assertEqual(fixture.watcher.product_terms(record), ["早教垫", "健身架"])
        self.assertFalse(fixture.watcher.matches_relevance(rows[0], record))
        self.assertTrue(fixture.watcher.matches_relevance(rows[1], record))


class MatcherTests(unittest.TestCase):
    def test_aliases_case_width_boundaries_and_missing_evidence(self):
        record = {"required_terms": ["novababy", "诺娃宝"]}
        for title in ["ＮＯＶＡＢＡＢＹ 游戏垫", "全新NovaBaby健身架", "诺娃宝垫子"]:
            self.assertTrue(fixture.watcher.matches_relevance({"title": title}, record), title)
        for title in ["Otherbrand 爬行垫", "", "notnovababy", "novababyplus"]:
            self.assertFalse(fixture.watcher.matches_relevance({"title": title}, record), title)
        self.assertFalse(fixture.watcher.matches_conditions({"title": "NovaBaby 垫子", "price": "240"}, {**record, "max_price": 240}))


if __name__ == "__main__":
    unittest.main()

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_assistant.core import (ResultStore, TravelError, criteria_values, eligible_text, map_links,
                                   offer_verdict, owner_key, query_values, rank_offers, travel_date, travel_intent)


TODAY = date(2026, 11, 1)


def offer(**overrides):
    value = {"kind": "train", "id": "G1", "service": "G1", "departure_time": "09:00", "arrival_time": "14:00",
             "arrival_date": "2026-11-07", "duration_minutes": 300, "price_cny": 550.0, "highspeed": True,
             "seat_type": "二等座", "available": True, "available_count": 2, "origin_code": "SHA", "destination_code": "PEK"}
    return {**value, **overrides}


class DateTests(unittest.TestCase):
    def test_relative_dates_and_sale_window(self):
        self.assertEqual(travel_date("明天", kind="train", today=TODAY), "2026-11-02")
        self.assertEqual(travel_date("2026-11-15", kind="train", today=TODAY), "2026-11-15")
        with self.assertRaises(TravelError) as error:
            travel_date("2026-11-16", kind="train", today=TODAY)
        self.assertEqual(error.exception.code, "outside_rail_sale_window")

    def test_bad_missing_past_and_far_future_dates(self):
        for value in (None, "下周", "2026-13-01", "2026-10-31", "2028-01-01"):
            with self.subTest(value=value), self.assertRaises(TravelError):
                travel_date(value, kind="flight", today=TODAY)

    def test_train_defaults_are_disclosed_and_flight_is_not_assumed_available(self):
        query = query_values("train", "上海", "北京", "明天", today=TODAY)
        self.assertEqual(query["criteria"], {"highspeed_only": True, "available_only": True, "seat_types": ["二等座"]})
        self.assertEqual(query_values("flight", "上海", "北京", "明天", today=TODAY)["criteria"], {})

    def test_query_rejects_type_same_place_and_incompatible_filters(self):
        cases = [("bus", "上海", "北京", {}), ("train", "上海", "上海", {}),
                 ("train", "上海", "北京", {"airlines": ["国航"]}),
                 ("flight", "上海", "北京", {"seat_types": ["二等座"]})]
        for kind, origin, destination, filters in cases:
            with self.subTest(kind=kind, filters=filters), self.assertRaises(TravelError):
                query_values(kind, origin, destination, "明天", filters, today=TODAY)


class CriteriaTests(unittest.TestCase):
    def test_bad_numbers_and_boolean_types_are_rejected(self):
        for filters in ({"max_price": True}, {"max_price": float("nan")}, {"max_duration_minutes": 5.5},
                        {"max_stops": -1}, {"min_tickets": 7}, {"nonstop_only": "true"}, {"unsupported": 1}):
            with self.subTest(filters=filters), self.assertRaises(TravelError):
                criteria_values(filters)

    def test_bad_time_windows_and_empty_lists_are_rejected(self):
        for filters in ({"depart_after": "25:00"}, {"depart_after": "9:00"},
                        {"depart_after": "23:00", "depart_before": "01:00"}, {"seat_types": []},
                        {"seat_types": ["经济舱"]}, {"arrival_airports": ["北京"]}, {"arrival_date": "tomorrow"}):
            with self.subTest(filters=filters), self.assertRaises(TravelError):
                criteria_values(filters)

    def test_filter_combines_price_departure_arrival_and_duration(self):
        offers = [offer(id="good"), offer(id="costly", price_cny=850), offer(id="early", departure_time="06:30"),
                  offer(id="late", arrival_time="20:00"), offer(id="slow", duration_minutes=490)]
        result = rank_offers(offers, {"max_price": 600, "depart_after": "08:00", "arrive_before": "18:00", "max_duration_minutes": 400}, "price")
        self.assertEqual([item["id"] for item in result["matching"]], ["good"])
        self.assertEqual(result["excluded_count"], 4)

    def test_unknown_price_does_not_satisfy_budget(self):
        result = rank_offers([offer(price_cny=None)], {"max_price": 600}, "price")
        self.assertEqual(result["matching_count"], 0)
        self.assertEqual(result["needs_verification"][0]["unverified_requirements"], ["max_price"])

    def test_unknown_inventory_does_not_guarantee_multiple_tickets(self):
        result = rank_offers([offer(available_count=None)], {"available_only": True, "min_tickets": 2}, "price")
        self.assertEqual(result["unverified_count"], 1)
        self.assertEqual(result["matching_count"], 0)

    def test_next_day_arrival_does_not_satisfy_same_day_constraint(self):
        result = rank_offers([offer(arrival_date="2026-11-08", arrival_time="02:00")],
                             {"arrive_before": "18:00", "arrival_date": "2026-11-07"}, "duration")
        self.assertEqual(result["excluded_count"], 1)

    def test_nonstop_airport_and_carrier_filters(self):
        good = offer(kind="flight", nonstop=True, stops=0, airline="China Eastern", origin_code="SHA", destination_code="PEK")
        filters = {"nonstop_only": True, "max_stops": 0, "airlines": ["China Eastern"], "departure_airports": ["SHA"]}
        self.assertEqual(offer_verdict(good, filters), ([], []))
        self.assertIn("nonstop_only", offer_verdict({**good, "nonstop": False}, filters)[0])
        self.assertIn("departure_airports", offer_verdict({**good, "origin_code": None}, filters)[1])

    def test_baggage_refund_unknowns_never_become_guarantees(self):
        result = rank_offers([offer(kind="flight", checked_baggage=None, refundable=None)],
                             {"checked_baggage_required": True, "refundable_required": True}, "price")
        self.assertEqual(set(result["needs_verification"][0]["unverified_requirements"]), {"checked_baggage_required", "refundable_required"})

    def test_sorting_keeps_unknown_prices_after_known_prices(self):
        offers = [offer(id="unknown", price_cny=None), offer(id="expensive", price_cny=900), offer(id="cheap", price_cny=400)]
        self.assertEqual([item["id"] for item in rank_offers(offers, {}, "price")["matching"]], ["cheap", "expensive", "unknown"])

    def test_known_failures_excluded_even_if_other_fields_unknown(self):
        result = rank_offers([offer(price_cny=None, available=False)], {"max_price": 600, "available_only": True}, "price")
        self.assertEqual(result["excluded_count"], 1)
        self.assertEqual(result["unverified_count"], 0)

    def test_excluded_airports_and_airlines_do_not_accept_unknowns(self):
        offers = [offer(id="wanted", origin_code="SHA", airline="China Eastern"),
                  offer(id="excluded", origin_code="PVG", airline="China Eastern"),
                  offer(id="unknown", origin_code=None, airline="China Eastern"),
                  offer(id="carrier", origin_code="SHA", airline="Spring Airlines")]
        result = rank_offers(offers, {"excluded_departure_airports": ["pvg"], "excluded_airlines": ["Spring"]}, "price")
        self.assertEqual([item["id"] for item in result["matching"]], ["wanted"])
        self.assertEqual(result["excluded_count"], 2)
        self.assertEqual(result["unverified_count"], 1)

    def test_all_carriers_must_satisfy_allowed_airlines(self):
        filters = {"airlines": ["China Eastern"]}
        self.assertEqual(offer_verdict(offer(airline="China Eastern"), filters), ([], []))
        self.assertEqual(offer_verdict(offer(airline="China Eastern and Air China"), filters)[0], ["airlines"])


class MapTests(unittest.TestCase):
    def test_search_and_route_urls_are_encoded_official_links(self):
        result = map_links("search", query="南京南站", city="南京")
        self.assertEqual(urlsplit(result["amap"]).hostname, "uri.amap.com")
        self.assertEqual(parse_qs(urlsplit(result["amap"]).query)["keyword"], ["南京南站"])
        route = map_links("route", origin="南京南站", destination="南京禄口机场", city="南京", mode="transit")
        self.assertFalse(route["live_route_metrics"])
        self.assertEqual(parse_qs(urlsplit(route["baidu_navigation"]).query)["origin"], ["南京南站"])

    def test_maps_reject_urls_credentials_and_incomplete_routes(self):
        for kwargs in ({"action": "route", "origin": "南京"}, {"action": "search", "query": "https://internal"},
                       {"action": "search", "query": "cookie=secret"}, {"action": "search", "query": "站", "mode": "fly"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TravelError):
                map_links(**kwargs)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = 1000
        self.store = ResultStore(Path(self.directory.name), clock=lambda: self.clock)
        self.owner = owner_key("group-one", "person-one")

    def test_results_are_bound_to_group_and_sender(self):
        identifier = self.store.add(self.owner, {}, [offer()], {})
        self.assertEqual(self.store.get(self.owner, identifier)["offers"][0]["id"], "G1")
        for owner in (owner_key("group-two", "person-one"), owner_key("group-one", "person-two")):
            with self.assertRaises(TravelError):
                self.store.get(owner, identifier)

    def test_expired_or_fabricated_results_fail_closed(self):
        identifier = self.store.add(self.owner, {}, [offer()], {})
        self.clock += 1201
        with self.assertRaises(TravelError):
            self.store.get(self.owner, identifier)
        with self.assertRaises(TravelError):
            self.store.get(self.owner, "../somewhere")

    def test_only_latest_result_for_owner_and_private_file_mode(self):
        first = self.store.add(self.owner, {"route": "first"}, [offer()], {})
        second = self.store.add(self.owner, {"route": "second"}, [offer()], {})
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.get(self.owner)["query"]["route"], "second")
        self.assertEqual((Path(self.directory.name) / "results.json").stat().st_mode & 0o777, 0o600)

    def test_refinement_keeps_previous_filters_without_extending_ttl(self):
        identifier = self.store.add(self.owner, {"criteria": {"max_price": 600}}, [offer()], {"observed_at": "old-time"})
        self.clock += 600
        self.store.refine(self.owner, {"criteria": {"max_price": 600, "depart_after": "08:00"}})
        saved = self.store.get(self.owner, identifier)
        self.assertEqual(saved["query"]["criteria"]["depart_after"], "08:00")
        self.assertEqual(saved["expires_at"], 2200)
        self.assertEqual(saved["metadata"]["observed_at"], "old-time")
        with self.assertRaises(TravelError):
            self.store.refine("another-owner", {}, identifier)

    def test_cache_evicts_oldest_before_exceeding_read_limit(self):
        for index in range(6):
            self.clock += 1
            self.store.add("owner-" + str(index), {}, [], {"sample": "a" * 900000})
        self.assertLessEqual((Path(self.directory.name) / "results.json").stat().st_size, 4 * 1024 * 1024)
        self.assertEqual(self.store.get("owner-5")["owner"], "owner-5")

    def test_corrupt_or_symlinked_cache_is_rejected(self):
        path = Path(self.directory.name) / "results.json"
        path.write_text("not-json")
        with self.assertRaises(TravelError):
            self.store.get(self.owner)
        path.unlink()
        path.symlink_to(Path(self.directory.name) / "elsewhere")
        with self.assertRaises(TravelError):
            self.store.get(self.owner)


class IntentTests(unittest.TestCase):
    def test_real_travel_requests_reserved_but_engineering_and_quotes_are_not(self):
        for text in ("帮我找明天上海到北京的机票", "导航去南京南站", "查上海到北京高铁"):
            self.assertTrue(travel_intent(text))
        for text in ("开发一个机票查询插件", "例如帮我找机票", "> 查高铁", "/travel help", "不要读取cookie"):
            self.assertFalse(travel_intent(text))


if __name__ == "__main__":
    unittest.main()

import asyncio
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from travel_assistant.core import TravelError, query_values
from travel_assistant.providers import (FLIGHTS, RAIL, FlightProvider, ProviderHTTP, RailProvider, fare_prices,
                                        flight_place, parse_flight_labels, parse_train_rows, resolve_station, station_catalogue)


TODAY = date(2026, 11, 1)
QUERY = query_values("train", "上海虹桥", "北京南", "2026-11-07", today=TODAY)
FLIGHT_QUERY = query_values("flight", "上海", "北京", "2026-11-07", today=TODAY)
LABEL = "From 1770 Chinese yuan. Nonstop flight with China Southern. Leaves Shanghai Pudong International Airport at 8:10 AM on Saturday, November 7 and arrives at Beijing Daxing International Airport at 10:35 AM on Saturday, November 7. Total duration 2 hr 25 min. Select flight"


def row(*, service="G532", depart="06:31", duration="05:47", quantity="有", sale="Y"):
    fields = [""] * 38
    for index, value in {2: "5l0000G53201", 3: service, 6: "AOH", 7: "VNP", 8: depart, 9: "12:18", 10: duration,
                         11: sale, 16: "01", 17: "09", 30: quantity, 31: "3", 32: "无", 35: "9MOO"}.items():
        fields[index] = value
    return "|".join(fields)


def rail_data(rows=None):
    return {"status": True, "data": {"result": [row()] if rows is None else rows, "map": {"AOH": "上海虹桥", "VNP": "北京南"}}}


class RailParsingTests(unittest.TestCase):
    def test_rows_expose_only_normalized_inventory_and_no_booking_secrets(self):
        raw = row().split("|")
        raw[0], raw[1] = "private-booking-secret", "预订"
        offers, metadata = parse_train_rows(rail_data(["|".join(raw)]), QUERY)
        self.assertEqual(offers[0]["service"], "G532")
        self.assertTrue(offers[0]["available"])
        self.assertIsNone(offers[0]["available_count"])
        self.assertNotIn("private-booking-secret", json.dumps(offers))

    def test_duplicate_rows_deduplicated(self):
        offers, metadata = parse_train_rows(rail_data([row(), row()]), QUERY)
        self.assertEqual(len(offers), 1)
        self.assertEqual(len(metadata), 1)

    def test_cross_midnight_arrival_uses_duration(self):
        offers, metadata = parse_train_rows(rail_data([row(depart="23:30", duration="03:20")]), QUERY)
        self.assertEqual(offers[0]["arrival_date"], "2026-11-08")
        self.assertEqual(offers[0]["arrival_time"], "02:50")

    def test_inventory_count_none_and_unrecognized_are_distinct(self):
        for quantity, available, count in (("3", True, 3), ("无", False, None), ("有", True, None), ("", None, None)):
            offers, _ = parse_train_rows(rail_data([row(quantity=quantity)]), QUERY)
            self.assertIs(offers[0]["available"], available)
            self.assertEqual(offers[0]["available_count"], count)

    def test_invalid_structure_is_not_reported_as_no_tickets(self):
        for data in ({"status": False}, {"status": True, "data": {"result": ["bad-row"]}}):
            with self.assertRaises(TravelError):
                parse_train_rows(data, QUERY)

    def test_invalid_clock_hours_fail_as_provider_error(self):
        for clock in ("24:00", "29:10", "12:60"):
            with self.subTest(clock=clock), self.assertRaises(TravelError):
                parse_train_rows(rail_data([row(depart=clock)]), QUERY)

    def test_zero_duration_train_is_not_accepted(self):
        with self.assertRaises(TravelError):
            parse_train_rows(rail_data([row(duration="00:00")]), QUERY)

    def test_fares_only_known_currency_fields_are_read(self):
        prices = fare_prices({"status": True, "data": {"O": "¥576.0", "A9": "¥1870.0", "9": "18700", "M": "unknown"}})
        self.assertEqual(prices, {"O": 576.0, "A9": 1870.0})

    def test_station_resolution_is_explicit_about_city_hub_and_accepts_suffix(self):
        stations = {"上海": {"code": "SHH", "city": "上海"}, "上海虹桥": {"code": "AOH", "city": "上海"}}
        self.assertEqual(resolve_station("上海虹桥站", stations, highspeed=True)[:2], ("上海虹桥", "AOH"))
        self.assertIn("不代表", resolve_station("上海", stations, highspeed=True)[2])
        self.assertEqual(resolve_station("SHH", stations, highspeed=True)[:2], ("上海", "SHH"))

    def test_catalogue_does_not_evaluate_javascript(self):
        raw = "var station_names='" + "".join(f"@abc|测试{index}|AAA|test|t|{index}|001|测试|||" for index in range(25)) + "';alert('no')"
        self.assertEqual(len(station_catalogue(raw.encode())), 25)


class FlightParsingTests(unittest.TestCase):
    def parse(self, label=LABEL, query=FLIGHT_QUERY):
        return parse_flight_labels(("<div aria-label='" + label + "'></div>").encode(), query, FLIGHTS)

    def test_prices_times_duration_and_airport_codes_are_normalized(self):
        offer = self.parse()[0]
        self.assertEqual((offer["price_cny"], offer["departure_time"], offer["arrival_time"], offer["duration_minutes"]), (1770, "08:10", "10:35", 145))
        self.assertEqual((offer["origin_code"], offer["destination_code"]), ("PVG", "PKX"))
        self.assertIsNone(offer["available"])
        self.assertIsNone(offer["checked_baggage"])

    def test_unknown_prices_and_unicode_spaces_are_preserved_as_unknown(self):
        offer = self.parse(LABEL.replace("From 1770 Chinese yuan.", "Total price is unavailable.").replace("8:10 AM", "8:10\u202fAM"))[0]
        self.assertIsNone(offer["price_cny"])

    def test_duplicate_dom_cards_are_deduplicated(self):
        text = ("<div aria-label='" + LABEL + "'></div>") * 2
        self.assertEqual(len(parse_flight_labels(text.encode(), FLIGHT_QUERY, FLIGHTS)), 1)

    def test_different_departure_date_is_rejected(self):
        with self.assertRaises(TravelError):
            self.parse(LABEL.replace("Saturday, November 7", "Sunday, November 8"))

    def test_connection_counts_and_overnight_are_not_hidden(self):
        label = LABEL.replace("Nonstop", "1 stop").replace("at 10:35 AM on Saturday, November 7", "at 1:35 AM on Sunday, November 8")
        offer = self.parse(label)[0]
        self.assertEqual(offer["stops"], 1)
        self.assertFalse(offer["nonstop"])
        self.assertEqual(offer["arrival_date"], "2026-11-08")

    def test_blocking_page_stops_without_fabricating_results(self):
        with self.assertRaises(TravelError) as error:
            parse_flight_labels(b"Our systems have detected unusual traffic", FLIGHT_QUERY, FLIGHTS)
        self.assertEqual(error.exception.code, "provider_restricted")

    def test_missing_cards_not_reported_as_no_flights(self):
        with self.assertRaises(TravelError):
            parse_flight_labels(b"<html>new frontend</html>", FLIGHT_QUERY, FLIGHTS)

    def test_city_and_airport_aliases(self):
        self.assertEqual(flight_place("上海"), "Shanghai")
        self.assertEqual(flight_place("上海浦东机场"), "PVG")
        self.assertEqual(flight_place("pvg"), "PVG")


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.http = ProviderHTTP()
        self.addAsyncCleanup(self.http.close)

    async def replace_client(self, handler, provider="rail"):
        target = "rail" if provider == "rail" else "flights"
        await getattr(self.http, target).aclose()
        setattr(self.http, target, httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False))

    async def test_429_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429)
        await self.replace_client(handler)
        with self.assertRaises(TravelError) as error:
            await self.http.fetch(RAIL + "leftTicket/init", provider="rail")
        self.assertEqual(error.exception.code, "provider_restricted")
        self.assertEqual(len(calls), 1)

    async def test_redirect_cannot_leave_provider_or_forward_to_loopback(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
        await self.replace_client(handler)
        with self.assertRaises(TravelError):
            await self.http.fetch(RAIL + "leftTicket/init", provider="rail")
        self.assertEqual(len(calls), 1)

    async def test_large_response_is_rejected(self):
        await self.replace_client(lambda request: httpx.Response(200, content=b"a" * 20))
        with self.assertRaises(TravelError):
            await self.http.fetch(RAIL + "leftTicket/init", provider="rail", limit=10)

    async def test_only_existing_loopback_proxy_allowed(self):
        for proxy in ("http://remote.example:7890", "http://user:secret@127.0.0.1:7890", "http://127.0.0.1:9000"):
            with self.assertRaises(TravelError):
                ProviderHTTP(flight_proxy=proxy)

    async def test_rail_declared_query_endpoint_is_followed_only_as_known_route(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RailProvider(self.http, Path(directory), max_price_queries=2)
            provider._stations = {"上海虹桥": {"code": "AOH"}, "北京南": {"code": "VNP"}}
            provider._bootstrapped = True
            self.http.fetch = AsyncMock(side_effect=[(json.dumps({"status": False, "c_url": "leftTicket/queryG"}).encode(), RAIL),
                                                     (json.dumps(rail_data()).encode(), RAIL),
                                                     (json.dumps({"status": True, "data": {"O": "¥576.0"}}).encode(), RAIL)])
            offers, metadata = await provider.search(QUERY)
            self.assertEqual(offers[0]["price_cny"], 576)
            self.assertIn("leftTicket/queryG?", self.http.fetch.call_args_list[1].args[0])

    async def test_foreign_rail_query_suggestion_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RailProvider(self.http, Path(directory))
            provider._stations = {"上海虹桥": {"code": "AOH"}, "北京南": {"code": "VNP"}}
            provider._bootstrapped = True
            self.http.fetch = AsyncMock(return_value=(json.dumps({"status": False, "c_url": "http://elsewhere/"}).encode(), RAIL))
            with self.assertRaises(TravelError):
                await provider.search(QUERY)
            self.assertEqual(self.http.fetch.await_count, 1)

    async def test_failed_fare_queries_do_not_claim_complete_price_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = RailProvider(self.http, Path(directory))
            provider._stations = {"上海虹桥": {"code": "AOH"}, "北京南": {"code": "VNP"}}
            provider._bootstrapped = True
            self.http.fetch = AsyncMock(side_effect=[(json.dumps(rail_data()).encode(), RAIL),
                                                     (json.dumps({"status": False}).encode(), RAIL)])
            offers, metadata = await provider.search(QUERY)
            self.assertIsNone(offers[0]["price_cny"])
            self.assertFalse(metadata["fare_coverage_complete"])
            self.assertEqual(metadata["verified_fares"], 0)

    async def test_flight_query_uses_explicit_airport_not_just_city(self):
        query = query_values("flight", "SHA", "PKX", "2026-11-07", today=TODAY)
        self.http.fetch = AsyncMock(return_value=(("<div aria-label='" + LABEL + "'></div>").encode(), FLIGHTS))
        with self.assertRaises(TravelError) as error:
            await FlightProvider(self.http).search(query)
        self.assertEqual(error.exception.code, "flight_airport_unverified")

    async def test_flight_page_from_different_city_is_not_accepted(self):
        query = query_values("flight", "南京", "北京", "2026-11-07", today=TODAY)
        self.http.fetch = AsyncMock(return_value=(("<div aria-label='" + LABEL + "'></div>").encode(), FLIGHTS))
        with self.assertRaises(TravelError) as error:
            await FlightProvider(self.http).search(query)
        self.assertEqual(error.exception.code, "flight_airport_unverified")

    async def test_station_cache_symlink_is_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "stations.json").symlink_to(root / "elsewhere")
            self.http.fetch = AsyncMock()
            with self.assertRaises(TravelError):
                await RailProvider(self.http, root).stations()
            self.http.fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

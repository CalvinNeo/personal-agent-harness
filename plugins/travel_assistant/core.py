from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.parse import quote, urlencode
import uuid
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
SEATS = ("二等座", "一等座", "商务座", "特等座", "硬座", "硬卧", "软卧", "无座")
SORTS = {"price", "duration", "departure"}
CRITERIA = {
    "max_price", "depart_after", "depart_before", "arrive_after", "arrive_before",
    "arrival_date", "max_duration_minutes", "nonstop_only", "max_stops", "highspeed_only",
    "available_only", "min_tickets", "seat_types", "airlines", "departure_airports",
    "arrival_airports", "excluded_airlines", "excluded_departure_airports", "excluded_arrival_airports",
    "checked_baggage_required", "refundable_required",
}


class TravelError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def place(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 80:
        raise TravelError("invalid_place", "请提供明确的城市、车站、机场或地点名称。")
    value = value.strip()
    if not re.fullmatch(r"[\w\u3400-\u9fff .·'()（）,，/+\-]+", value) or any(
        word in value.lower() for word in ("http", "token", "cookie", "password", "authorization")
    ):
        raise TravelError("invalid_place", "地点只接受名称或机场代码，不接受网址、凭据或指令。")
    return value


def travel_date(value: Any, *, kind: str, today: date | None = None) -> str:
    today = today or datetime.now(TZ).date()
    if not isinstance(value, str):
        raise TravelError("missing_date", "请明确出发日期。")
    relative = {"今天": 0, "明天": 1, "后天": 2}
    try:
        parsed = today + timedelta(days=relative[value]) if value in relative else date.fromisoformat(value)
    except (ValueError, TypeError):
        raise TravelError("invalid_date", "日期请使用 YYYY-MM-DD，或今天、明天、后天。") from None
    if parsed < today or parsed > today + timedelta(days=330):
        raise TravelError("invalid_date", "出发日期必须在今天至未来 330 天之内。")
    if kind == "train" and parsed > today + timedelta(days=14):
        raise TravelError("outside_rail_sale_window", "该日期尚未进入 12306 通常的 15 天预售期；不能把尚未开售说成无票。")
    return parsed.isoformat()


def criteria_values(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - CRITERIA:
        raise TravelError("invalid_criteria", "筛选项无效，请使用预算、时间、席别、航司等支持的条件。")
    result = dict(value)
    for key in ("max_price", "max_duration_minutes", "max_stops", "min_tickets"):
        if key not in result:
            continue
        number = result[key]
        ceiling = {"max_price": 1000000, "max_duration_minutes": 10080, "max_stops": 3, "min_tickets": 6}[key]
        minimum = 0 if key == "max_stops" else 1
        if type(number) not in (int, float) or not math.isfinite(number) or not minimum <= number <= ceiling:
            raise TravelError("invalid_criteria", "预算、时长、人数或中转次数超出允许范围。")
        if key != "max_price" and type(number) is not int:
            raise TravelError("invalid_criteria", "时长、人数和中转次数必须是整数。")
    for key in ("nonstop_only", "highspeed_only", "available_only", "checked_baggage_required", "refundable_required"):
        if key in result and type(result[key]) is not bool:
            raise TravelError("invalid_criteria", "开关筛选项必须是布尔值。")
    for key in ("depart_after", "depart_before", "arrive_after", "arrive_before"):
        if key in result and (not isinstance(result[key], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result[key])):
            raise TravelError("invalid_criteria", "时间须为 24 小时制 HH:MM。")
    for lower, upper in (("depart_after", "depart_before"), ("arrive_after", "arrive_before")):
        if result.get(lower) and result.get(upper) and result[lower] > result[upper]:
            raise TravelError("invalid_criteria", "时间窗跨越午夜时，请拆分查询并明确日期。")
    if "arrival_date" in result:
        try:
            date.fromisoformat(result["arrival_date"])
        except (ValueError, TypeError):
            raise TravelError("invalid_criteria", "到达日期须为 YYYY-MM-DD。") from None
    for key in ("seat_types", "airlines", "departure_airports", "arrival_airports",
                "excluded_airlines", "excluded_departure_airports", "excluded_arrival_airports"):
        if key not in result:
            continue
        items = result[key]
        if not isinstance(items, list) or not 1 <= len(items) <= 8:
            raise TravelError("invalid_criteria", "席别、航司和机场条件须为 1 至 8 项列表。")
        result[key] = [place(item) for item in items]
        if key == "seat_types" and any(item not in SEATS for item in items):
            raise TravelError("invalid_criteria", "不支持该席别，请使用二等座、一等座、商务座等明确名称。")
        if key.endswith("airports"):
            if any(not re.fullmatch("[A-Za-z]{3}", item) for item in items):
                raise TravelError("invalid_criteria", "机场筛选请使用 PVG、SHA、PEK 等三字码。")
            result[key] = [item.upper() for item in items]
    return result


def query_values(kind: Any, origin: Any, destination: Any, departure_date: Any,
                 criteria: Any = None, sort: Any = "price", *, today: date | None = None) -> dict:
    if kind not in {"train", "flight"}:
        raise TravelError("invalid_kind", "票务类型只能是 train 或 flight。")
    if sort not in SORTS:
        raise TravelError("invalid_sort", "排序只能选价格、时长或出发时间。")
    start, end = place(origin), place(destination)
    if start.casefold() == end.casefold():
        raise TravelError("same_place", "出发地和目的地不能相同。")
    filters = criteria_values(criteria)
    if kind == "train":
        unsupported = {"nonstop_only", "max_stops", "airlines", "departure_airports", "arrival_airports",
                       "excluded_airlines", "excluded_departure_airports", "excluded_arrival_airports",
                       "checked_baggage_required", "refundable_required"} & set(filters)
        if unsupported:
            raise TravelError("unsupported_criteria", "火车查询不支持航司、机场或机票行李退改条件。")
        filters.setdefault("highspeed_only", True)
        filters.setdefault("available_only", True)
        filters.setdefault("seat_types", ["二等座"])
    elif {"highspeed_only", "seat_types"} & set(filters):
        raise TravelError("unsupported_criteria", "机票请使用航司、机场、直飞等条件，不使用火车席别。")
    return {"kind": kind, "origin": start, "destination": end,
            "departure_date": travel_date(departure_date, kind=kind, today=today), "criteria": filters, "sort": sort}


def offer_verdict(offer: dict, criteria: dict) -> tuple[list[str], list[str]]:
    rejected, unknown = [], []

    def compare(field, key, predicate):
        if key not in criteria:
            return
        actual = offer.get(field)
        if actual is None:
            unknown.append(key)
        elif not predicate(actual, criteria[key]):
            rejected.append(key)

    compare("price_cny", "max_price", lambda actual, wanted: actual <= wanted)
    compare("duration_minutes", "max_duration_minutes", lambda actual, wanted: actual <= wanted)
    compare("departure_time", "depart_after", lambda actual, wanted: actual >= wanted)
    compare("departure_time", "depart_before", lambda actual, wanted: actual <= wanted)
    compare("arrival_time", "arrive_after", lambda actual, wanted: actual >= wanted)
    compare("arrival_time", "arrive_before", lambda actual, wanted: actual <= wanted)
    compare("arrival_date", "arrival_date", lambda actual, wanted: actual == wanted)
    compare("stops", "max_stops", lambda actual, wanted: actual <= wanted)
    compare("available_count", "min_tickets", lambda actual, wanted: actual >= wanted)
    compare("seat_type", "seat_types", lambda actual, wanted: actual in wanted)
    compare("airline", "airlines", lambda actual, wanted: all(
        any(item.casefold() in carrier.casefold() for item in wanted)
        for carrier in re.split(r",\s*|\s+and\s+|\s*&\s*|\s*/\s*", actual)
    ))
    compare("airline", "excluded_airlines", lambda actual, wanted: not any(item.casefold() in actual.casefold() for item in wanted))
    compare("origin_code", "departure_airports", lambda actual, wanted: actual in wanted)
    compare("destination_code", "arrival_airports", lambda actual, wanted: actual in wanted)
    compare("origin_code", "excluded_departure_airports", lambda actual, wanted: actual not in wanted)
    compare("destination_code", "excluded_arrival_airports", lambda actual, wanted: actual not in wanted)
    for key, field in (("nonstop_only", "nonstop"), ("highspeed_only", "highspeed"),
                       ("available_only", "available"), ("checked_baggage_required", "checked_baggage"),
                       ("refundable_required", "refundable")):
        if criteria.get(key):
            compare(field, key, lambda actual, wanted: actual is True)
    return rejected, unknown


def rank_offers(offers: list[dict], criteria: dict, sort: str, *, limit: int = 8) -> dict:
    filters = criteria_values(criteria)
    if sort not in SORTS or type(limit) is not int or not 1 <= limit <= 10:
        raise TravelError("invalid_sort", "排序或结果数量无效。")
    matching, uncertain, rejected_count = [], [], 0
    for offer in offers:
        rejected, unknown = offer_verdict(offer, filters)
        if rejected:
            rejected_count += 1
        elif unknown:
            uncertain.append({**offer, "unverified_requirements": unknown})
        else:
            matching.append(offer)
    field = {"price": "price_cny", "duration": "duration_minutes", "departure": "departure_time"}[sort]

    def key(offer):
        value = offer.get(field)
        return value is None, value if value is not None else "", offer.get("departure_time") or ""

    matching.sort(key=key)
    uncertain.sort(key=key)
    return {"matching": matching[:limit], "needs_verification": uncertain[:limit],
            "matching_count": len(matching), "unverified_count": len(uncertain),
            "excluded_count": rejected_count, "checked_count": len(offers), "criteria": filters, "sort": sort}


def map_links(action: Any, *, query: Any = "", origin: Any = "", destination: Any = "",
              city: Any = "", mode: Any = "transit") -> dict:
    if mode not in {"car", "transit", "walk", "bike"} or action not in {"search", "route"}:
        raise TravelError("invalid_map_request", "地图支持地点搜索，或驾车、公交、步行、骑行路线。")
    region = place(city) if city else ""
    result = {"ok": True, "live_route_metrics": False,
              "notice": "这是官方地图查询/导航入口；实时路况、距离、耗时和公交班次请打开地图查看，未读取你的当前位置。"}
    if action == "search":
        keyword = place(query)
        params = {"keyword": keyword, "city": region, "view": "list", "src": "CalvinBotTravel"}
        result.update({"query": keyword, "amap": "https://uri.amap.com/search?" + urlencode(params),
                       "baidu": "https://map.baidu.com/search/" + quote((region + " " + keyword).strip(), safe="")})
    else:
        start, end = place(origin), place(destination)
        result.update({"origin": start, "destination": end, "mode": mode})
        params = {"origin": start, "destination": end, "mode": {"car": "driving", "transit": "transit", "walk": "walking", "bike": "riding"}[mode],
                  "output": "html", "src": "CalvinBotTravel"}
        if region:
            params["region"] = region
        result["baidu_navigation"] = "https://api.map.baidu.com/direction?" + urlencode(params)
        result["amap_destination"] = "https://uri.amap.com/search?" + urlencode({"keyword": end, "city": region, "src": "CalvinBotTravel"})
    return result


def eligible_text(text: Any) -> bool:
    if not isinstance(text, str) or not 2 <= len(text.strip()) <= 3000:
        return False
    text = text.strip()
    return not (text.startswith(("/", ">", "```", '"', "“", "「", "『")) or re.match(
        r"^(?:他说|她说|引用|转发|例如|比如|测试文本|假如|如果我说)", text
    ))


def travel_intent(text: str) -> bool:
    return bool(eligible_text(text) and re.search(r"地图|导航|路线|怎么去|高铁|动车|火车票|机票|航班|车次|出行|12306", text, re.I)
                and not re.search(r"开发|编写|实现|集成|源码|代码|插件|(?:修复|修改).{0,10}(?:程序|服务|工具)", text))


def owner_key(umo: str, sender: str) -> str:
    return hashlib.sha256((umo + "\x00" + sender).encode()).hexdigest()


class ResultStore:
    def __init__(self, root: Path, *, ttl: int = 1200, clock=time.time):
        self.root, self.ttl, self.clock = Path(root), ttl, clock

    def _read(self) -> dict:
        path = self.root / "results.json"
        if path.is_symlink():
            raise TravelError("unsafe_state", "出行缓存目录不安全。")
        if not path.exists():
            return {}
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError
            records = json.loads(path.read_text())
            if not isinstance(records, dict):
                raise ValueError
            return {key: item for key, item in records.items() if isinstance(item, dict) and
                    type(item.get("expires_at")) in (int, float) and item["expires_at"] > self.clock()}
        except (ValueError, OSError):
            raise TravelError("state_unavailable", "出行结果缓存不可用，请重新查询。") from None

    def _write(self, records):
        while len(json.dumps(records, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
            if len(records) <= 1:
                raise TravelError("state_too_large", "本次结果过大，无法安全缓存，请缩小查询范围。")
            oldest = min(records, key=lambda key: records[key]["expires_at"])
            del records[oldest]
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".travel-", dir=self.root)
        try:
            with os.fdopen(descriptor, "w") as output:
                json.dump(records, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / "results.json")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def add(self, owner: str, query: dict, offers: list[dict], metadata: dict) -> str:
        records = self._read()
        identifier = uuid.uuid4().hex
        records = {key: item for key, item in records.items() if item["owner"] != owner}
        records[identifier] = {"owner": owner, "query": query, "offers": offers[:600], "metadata": metadata,
                               "expires_at": self.clock() + self.ttl}
        if len(records) > 40:
            records = dict(sorted(records.items(), key=lambda pair: pair[1]["expires_at"])[-40:])
        self._write(records)
        return identifier

    def get(self, owner: str, identifier: str = "") -> dict:
        if identifier and not re.fullmatch("[a-f0-9]{32}", identifier):
            raise TravelError("unknown_result_set", "结果编号无效，请重新查询。")
        records = self._read()
        item = records.get(identifier) if identifier else next((item for item in records.values() if item["owner"] == owner), None)
        if item is None or item["owner"] != owner:
            raise TravelError("unknown_result_set", "当前会话没有属于你的有效结果，请重新搜索；不会读取其他人的查询。")
        return item

    def refine(self, owner: str, query: dict, identifier: str = "") -> str:
        self.get(owner, identifier)
        records = self._read()
        identifier = identifier or next((key for key, item in records.items() if item["owner"] == owner), "")
        if identifier not in records or records[identifier]["owner"] != owner:
            raise TravelError("unknown_result_set", "结果已过期，请重新搜索。")
        records[identifier]["query"] = query
        self._write(records)
        return identifier

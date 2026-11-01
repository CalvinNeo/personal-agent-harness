from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from .core import TZ, TravelError, offer_verdict, place


RAIL = "https://kyfw.12306.cn/otn/"
FLIGHTS = "https://www.google.com/travel/flights"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/" + ".".join(("131", "0", "0", "0")) + " Safari/537.36"
SEAT_FIELDS = {"二等座": (30, "O"), "一等座": (31, "M"), "商务座": (32, "A9"),
               "特等座": (25, "P"), "硬座": (29, "A1"), "硬卧": (28, "A3"), "软卧": (23, "A4"), "无座": (26, "WZ")}
RAIL_HUBS = {"上海": "上海虹桥", "北京": "北京南", "南京": "南京南", "杭州": "杭州东", "广州": "广州南",
             "深圳": "深圳北", "天津": "天津南", "武汉": "武汉", "成都": "成都东", "重庆": "重庆北",
             "西安": "西安北", "合肥": "合肥南", "郑州": "郑州东", "济南": "济南西", "长沙": "长沙南",
             "福州": "福州南", "厦门": "厦门北", "苏州": "苏州北", "无锡": "无锡东", "南昌": "南昌西"}
FLIGHT_PLACES = {
    "上海": "Shanghai", "北京": "Beijing", "南京": "Nanjing", "广州": "Guangzhou", "深圳": "Shenzhen",
    "杭州": "Hangzhou", "成都": "Chengdu", "重庆": "Chongqing", "西安": "Xi'an", "武汉": "Wuhan",
    "天津": "Tianjin", "青岛": "Qingdao", "济南": "Jinan", "长沙": "Changsha", "厦门": "Xiamen",
    "福州": "Fuzhou", "昆明": "Kunming", "海口": "Haikou", "三亚": "Sanya", "郑州": "Zhengzhou",
    "哈尔滨": "Harbin", "沈阳": "Shenyang", "大连": "Dalian", "长春": "Changchun", "太原": "Taiyuan",
    "石家庄": "Shijiazhuang", "南宁": "Nanning", "贵阳": "Guiyang", "乌鲁木齐": "Urumqi", "拉萨": "Lhasa",
    "兰州": "Lanzhou", "银川": "Yinchuan", "西宁": "Xining", "呼和浩特": "Hohhot", "宁波": "Ningbo",
    "温州": "Wenzhou", "南昌": "Nanchang", "合肥": "Hefei", "无锡": "Wuxi", "泉州": "Quanzhou",
    "香港": "Hong Kong", "澳门": "Macau", "台北": "Taipei", "东京": "Tokyo", "大阪": "Osaka",
    "首尔": "Seoul", "新加坡": "Singapore", "曼谷": "Bangkok", "伦敦": "London", "巴黎": "Paris",
    "纽约": "New York", "洛杉矶": "Los Angeles", "悉尼": "Sydney", "墨尔本": "Melbourne",
}
AIRPORTS = {
    "PVG": ("上海浦东", "上海浦东机场", "上海浦东国际机场", "Shanghai Pudong International Airport"),
    "SHA": ("上海虹桥机场", "上海虹桥国际机场", "Shanghai Hongqiao International Airport"),
    "PEK": ("北京首都机场", "北京首都国际机场", "Beijing Capital International Airport"),
    "PKX": ("北京大兴机场", "北京大兴国际机场", "Beijing Daxing International Airport"),
    "NKG": ("南京禄口机场", "南京禄口国际机场", "Nanjing Lukou International Airport"),
    "CAN": ("广州白云机场", "Guangzhou Baiyun International Airport"),
    "SZX": ("深圳宝安机场", "Shenzhen Bao'an International Airport"),
    "HGH": ("杭州萧山机场", "Hangzhou Xiaoshan International Airport"),
    "TFU": ("成都天府机场", "Chengdu Tianfu International Airport"),
    "CTU": ("成都双流机场", "Chengdu Shuangliu International Airport"),
    "CKG": ("重庆江北机场", "Chongqing Jiangbei International Airport"),
    "XIY": ("西安咸阳机场", "Xi'an Xianyang International Airport"),
    "HKG": ("香港机场", "Hong Kong International Airport"),
    "TPE": ("台北桃园机场", "Taiwan Taoyuan International Airport"),
    "NRT": ("东京成田机场", "Narita International Airport"), "HND": ("东京羽田机场", "Haneda Airport"),
    "KIX": ("大阪关西机场", "Kansai International Airport"), "SIN": ("新加坡樟宜机场", "Singapore Changi Airport"),
    "ICN": ("仁川机场", "Incheon International Airport"), "BKK": ("曼谷素万那普机场", "Suvarnabhumi Airport"),
    "LHR": ("伦敦希思罗机场", "Heathrow Airport"), "CDG": ("巴黎戴高乐机场", "Paris Charles de Gaulle Airport"),
    "JFK": ("纽约肯尼迪机场", "John F. Kennedy International Airport"), "LAX": ("洛杉矶机场", "Los Angeles International Airport"),
}
AIRLINE_NAMES = {"国航": "Air China", "中国国航": "Air China", "中国国际航空": "Air China",
                 "东航": "China Eastern", "东方航空": "China Eastern", "中国东方航空": "China Eastern",
                 "南航": "China Southern", "南方航空": "China Southern", "中国南方航空": "China Southern",
                 "海航": "Hainan", "海南航空": "Hainan", "春秋": "Spring", "春秋航空": "Spring",
                 "吉祥": "Juneyao", "吉祥航空": "Juneyao", "深航": "Shenzhen", "深圳航空": "Shenzhen",
                 "厦航": "Xiamen", "厦门航空": "Xiamen"}
CITY_AIRPORTS = {"Shanghai": {"PVG", "SHA"}, "Beijing": {"PEK", "PKX"}, "Chengdu": {"TFU", "CTU"},
                 "Tokyo": {"NRT", "HND"}, "Osaka": {"KIX"}, "London": {"LHR"}, "Paris": {"CDG"},
                 "New York": {"JFK"}, "Los Angeles": {"LAX"}, "Taipei": {"TPE"}, "Seoul": {"ICN"},
                 "Singapore": {"SIN"}, "Bangkok": {"BKK"}}


class ProviderHTTP:
    def __init__(self, *, flight_proxy: str = ""):
        if flight_proxy not in {"", "http://127.0.0.1:7890"}:
            raise TravelError("invalid_proxy", "机票代理只允许使用现有本机代理。")
        options = {"timeout": httpx.Timeout(22, connect=8), "follow_redirects": False, "trust_env": False,
                   "headers": {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                   "limits": httpx.Limits(max_connections=4, max_keepalive_connections=2)}
        self.rail = httpx.AsyncClient(**options)
        self.flights = httpx.AsyncClient(proxy=flight_proxy or None, **options)

    async def fetch(self, url: str, *, provider: str, limit: int = 3 * 1024 * 1024) -> tuple[bytes, str]:
        client = self.rail if provider == "rail" else self.flights
        allowed = "kyfw.12306.cn" if provider == "rail" else "www.google.com"
        try:
            async with asyncio.timeout(32):
                for hop in range(4):
                    parsed = urlsplit(url)
                    if parsed.scheme != "https" or parsed.hostname != allowed or parsed.port not in (None, 443) or parsed.username or parsed.password:
                        raise TravelError("provider_redirect_rejected", "数据源跳转到未批准的地址，已停止请求。")
                    if provider == "flight" and parsed.path.startswith(("/sorry", "/travel/flights/unsupported")):
                        raise TravelError("provider_restricted", "机票数据源要求验证或不支持当前请求，未尝试绕过。")
                    headers = {"Referer": RAIL + "leftTicket/init"} if provider == "rail" else {}
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            if not location or hop == 3:
                                raise TravelError("provider_redirect_rejected", "数据源跳转过多，查询未完成。")
                            url = urljoin(url, location)
                            continue
                        if response.status_code in {403, 429}:
                            raise TravelError("provider_restricted", "数据源拒绝或限流，本次不重试、不绕过；请打开官方入口确认。")
                        if response.status_code != 200:
                            raise TravelError("provider_unavailable", "票务数据源暂时不可用，不能据此判断无票。")
                        size = response.headers.get("Content-Length", "")
                        if size.isdecimal() and int(size) > limit:
                            raise TravelError("provider_response_too_large", "数据源响应过大，已停止读取。")
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > limit:
                                raise TravelError("provider_response_too_large", "数据源响应过大，已停止读取。")
                            chunks.append(chunk)
                        return b"".join(chunks), url
        except (httpx.HTTPError, TimeoutError):
            raise TravelError("provider_unavailable", "票务数据源连接失败或超时，不能据此判断无票。") from None
        raise TravelError("provider_unavailable", "票务查询未完成。")

    async def close(self):
        await self.rail.aclose()
        await self.flights.aclose()


def station_catalogue(raw: bytes) -> dict[str, dict]:
    text = raw.decode("utf-8-sig")
    entries = {}
    for record in text.split("@"):
        fields = record.split("|")
        if len(fields) >= 8 and re.fullmatch("[A-Z]{3}", fields[2]) and 1 <= len(fields[1]) <= 30:
            entries[fields[1]] = {"code": fields[2], "city": fields[7]}
    if len(entries) < 20:
        raise TravelError("station_data_unavailable", "12306 车站目录格式已变化，请稍后再查。")
    return entries


def resolve_station(value: str, stations: dict, *, highspeed: bool) -> tuple[str, str, str | None]:
    name = place(value)
    if name.endswith("站") and name[:-1] in stations:
        name = name[:-1]
    selected = RAIL_HUBS.get(name, name) if highspeed else name
    if selected in stations:
        note = f"{name}按{selected}查询，不代表该城市全部车站。" if selected != name else None
        return selected, stations[selected]["code"], note
    if re.fullmatch("[A-Za-z]{3}", name):
        found = next(((key, item["code"]) for key, item in stations.items() if item["code"] == name.upper()), None)
        if found:
            return found[0], found[1], None
    candidates = [key for key, item in stations.items() if name in key or item["city"] == name][:8]
    raise TravelError("station_not_found", "请使用明确车站名或电报码。" + ("可选：" + "、".join(candidates) if candidates else ""))


def parse_train_rows(data: dict, query: dict) -> tuple[list[dict], dict]:
    if data.get("status") is not True or not isinstance(data.get("data"), dict) or not isinstance(data["data"].get("result"), list):
        raise TravelError("rail_response_invalid", "12306 未返回有效班次；这不是无票结论。")
    names = data["data"].get("map") or {}
    offers, metadata, seen = [], {}, set()
    selected = query["criteria"].get("seat_types", ["二等座"])
    for raw in data["data"]["result"][:500]:
        fields = raw.split("|") if isinstance(raw, str) else []
        if len(fields) < 36 or not re.fullmatch(r"[A-Z]?\d{1,5}", fields[3]) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", fields[8]):
            continue
        duration = re.fullmatch(r"(\d{1,3}):([0-5]\d)", fields[10])
        if not duration:
            continue
        minutes = int(duration[1]) * 60 + int(duration[2])
        if not 1 <= minutes <= 10080:
            continue
        departure = datetime.fromisoformat(query["departure_date"] + "T" + fields[8])
        arrival = departure + timedelta(minutes=minutes)
        train_key = ":".join((fields[2], fields[6], fields[7], fields[8]))
        if not re.fullmatch(r"[A-Za-z0-9]{5,30}", fields[2]) or not all(re.fullmatch(r"\d{1,3}", fields[index]) for index in (16, 17)):
            continue
        metadata[train_key] = {"train_no": fields[2], "from_station_no": fields[16], "to_station_no": fields[17], "seat_types": fields[35]}
        for seat in selected:
            identity = train_key + ":" + seat
            if identity in seen:
                continue
            seen.add(identity)
            position, price_key = SEAT_FIELDS[seat]
            quantity = fields[position]
            count = int(quantity) if quantity.isdecimal() else None
            available = (quantity == "有" or count is not None and count > 0) if quantity in {"有", "无", "--"} or count is not None else None
            if fields[11] != "Y":
                available = False
            offers.append({"kind": "train", "id": identity, "fare_reference": train_key, "service": fields[3],
                           "origin": names.get(fields[6], fields[6]), "destination": names.get(fields[7], fields[7]),
                           "origin_code": fields[6], "destination_code": fields[7],
                           "departure_date": query["departure_date"], "departure_time": fields[8],
                           "arrival_date": arrival.date().isoformat(), "arrival_time": arrival.strftime("%H:%M"),
                           "duration_minutes": minutes, "highspeed": fields[3].startswith(("G", "D", "C")),
                           "seat_type": seat, "available": available, "available_count": count,
                           "availability_text": quantity or "未知", "price_cny": None, "currency": "CNY",
                           "fare_key": price_key, "price_kind": "12306_queried_fare", "source": RAIL + "leftTicket/init",
                           "booking_url": RAIL + "leftTicket/init"})
    if data["data"]["result"] and not offers:
        raise TravelError("rail_response_invalid", "12306 班次结构无法识别，本次不返回猜测结果。")
    return offers, metadata


def fare_prices(data: dict) -> dict[str, float]:
    if data.get("status") is not True or not isinstance(data.get("data"), dict):
        return {}
    prices = {}
    for key, value in data["data"].items():
        if key not in {"O", "M", "A9", "P", "A1", "A3", "A4", "WZ"} or not isinstance(value, str):
            continue
        match = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d{1,2})?)", value)
        if match and float(match[1]) > 0:
            prices[key] = float(match[1])
    return prices


class RailProvider:
    def __init__(self, http: ProviderHTTP, data_root: Path, *, max_price_queries: int = 24):
        self.http, self.data_root, self.max_price_queries = http, Path(data_root), max_price_queries
        self._stations = None
        self._bootstrapped = False

    async def stations(self):
        if self._stations:
            return self._stations
        path = self.data_root / "stations.json"
        if path.is_symlink():
            raise TravelError("unsafe_state", "车站缓存文件不安全，请检查数据目录。")
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 2 * 1024 * 1024 and time.time() - path.stat().st_mtime < 86400:
            try:
                data = json.loads(path.read_text())
                if isinstance(data, dict) and len(data) > 20 and all(isinstance(item, dict) and re.fullmatch("[A-Z]{3}", str(item.get("code", ""))) for item in data.values()):
                    self._stations = data
                    return data
            except (ValueError, OSError):
                pass
        raw, _ = await self.http.fetch(RAIL + "resources/js/framework/station_name.js", provider="rail")
        self._stations = station_catalogue(raw)
        self.data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(self._stations, ensure_ascii=False))
        path.chmod(0o600)
        return self._stations

    async def search(self, query: dict) -> tuple[list[dict], dict]:
        stations = await self.stations()
        origin, from_code, from_note = resolve_station(query["origin"], stations, highspeed=query["criteria"]["highspeed_only"])
        destination, to_code, to_note = resolve_station(query["destination"], stations, highspeed=query["criteria"]["highspeed_only"])
        if from_code == to_code:
            raise TravelError("same_place", "解析后的起终站相同，请确认车站。")
        if not self._bootstrapped:
            await self.http.fetch(RAIL + "leftTicket/init", provider="rail")
            self._bootstrapped = True
        params = {"leftTicketDTO.train_date": query["departure_date"], "leftTicketDTO.from_station": from_code,
                  "leftTicketDTO.to_station": to_code, "purpose_codes": "ADULT"}
        raw, _ = await self.http.fetch(RAIL + "leftTicket/query?" + urlencode(params), provider="rail")
        try:
            data = json.loads(raw)
            if data.get("status") is False and data.get("c_url") == "leftTicket/queryG":
                raw, _ = await self.http.fetch(RAIL + "leftTicket/queryG?" + urlencode(params), provider="rail")
                data = json.loads(raw)
        except (ValueError, AttributeError):
            raise TravelError("rail_response_invalid", "12306 返回了错误页或验证页，不能据此判断无票；未尝试绕过。") from None
        offers, trains = parse_train_rows(data, query)
        eligible = []
        prefilters = {key: value for key, value in query["criteria"].items() if key != "max_price"}
        for offer in offers:
            rejected, _ = offer_verdict(offer, prefilters)
            if not rejected and offer["fare_reference"] not in eligible:
                eligible.append(offer["fare_reference"])
        chosen = eligible[:self.max_price_queries]
        semaphore = asyncio.Semaphore(3)

        async def price(service):
            async with semaphore:
                try:
                    params = {**trains[service], "train_date": query["departure_date"]}
                    raw, _ = await self.http.fetch(RAIL + "leftTicket/queryTicketPrice?" + urlencode(params), provider="rail")
                    return service, fare_prices(json.loads(raw))
                except TravelError as error:
                    if error.code == "provider_restricted":
                        raise
                    return service, {}
                except (ValueError, TypeError):
                    return service, {}

        tasks = [asyncio.create_task(price(service)) for service in chosen]
        try:
            prices = dict(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        observed = datetime.now(TZ).isoformat(timespec="seconds")
        for offer in offers:
            offer["price_cny"] = prices.get(offer.pop("fare_reference"), {}).get(offer.pop("fare_key"))
            offer["observed_at"] = observed
        notices = [note for note in (from_note, to_note) if note]
        notices += ["查询为单程、成人、指定席别；余票和价格会变化，最终以 12306 为准。", "只查询同车直达班次，暂不组合中转换乘。"]
        eligible_offers = [offer for offer in offers if not offer_verdict(offer, prefilters)[0]]
        verified_fares = sum(offer["price_cny"] is not None for offer in eligible_offers)
        complete = verified_fares == len(eligible_offers)
        if not complete:
            notices.append(f"本次对 {len(chosen)} 个班次请求核价，共 {len(eligible)} 个班次通过非价格条件；取得 {verified_fares} 个席别报价，未核价项不会冒充符合预算，不保证全量最低价。")
        return offers, {"provider": "12306", "source": RAIL + "leftTicket/init", "observed_at": observed,
                        "stations": {"origin": origin, "destination": destination}, "queried_trains": len(trains),
                        "fare_requests": len(chosen), "verified_fares": verified_fares, "fare_coverage_complete": complete, "notices": notices}


def flight_place(value: str) -> str:
    value = place(value)
    if value in FLIGHT_PLACES:
        return FLIGHT_PLACES[value]
    for code, names in AIRPORTS.items():
        if value in names or value.upper() == code:
            return code
    return value


def airport_code(name: str) -> str | None:
    normalized = name.casefold()
    return next((code for code, names in AIRPORTS.items() if normalized in {item.casefold() for item in names}), None)


def flight_endpoint_matches(requested: str, airport: str, code: str | None) -> bool:
    resolved = flight_place(requested)
    if re.fullmatch("[A-Z]{3}", resolved):
        return code == resolved
    if code in CITY_AIRPORTS.get(resolved, set()):
        return True
    return bool(resolved.isascii() and re.search(r"(?<!\w)" + re.escape(resolved) + r"(?!\w)", airport, re.I))


def flight_search_url(query: dict) -> str:
    phrase = f"Flights from {flight_place(query['origin'])} to {flight_place(query['destination'])} on {query['departure_date']} one way economy 1 adult"
    return FLIGHTS + "?" + urlencode({"q": phrase, "hl": "en", "curr": "CNY"})


class FlightLabels(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels = []
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        label = dict(attrs).get("aria-label", "")
        if "Leaves " in label and "Select flight" in label and len(label) < 4000:
            self.labels.append(" ".join(label.split()))


def parse_flight_labels(raw: bytes, query: dict, source: str) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    if any(marker in text.lower() for marker in ("our systems have detected unusual traffic", "before you continue to google", "/sorry/index")):
        raise TravelError("provider_restricted", "机票数据源要求验证，本次不重试或绕过。")
    parser = FlightLabels()
    parser.feed(text)
    offers, seen = [], set()
    observed = datetime.now(TZ).isoformat(timespec="seconds")
    for label in parser.labels[:400]:
        timing = re.search(r"Leaves (.+?) at (\d{1,2}:\d{2} [AP]M) on (.+?) and arrives at (.+?) at (\d{1,2}:\d{2} [AP]M) on (.+?)\. Total duration (.+?)\.", label)
        carrier = re.search(r"(Nonstop|\d+ stops?) flight with (.+?)\. Leaves ", label)
        if not timing or not carrier:
            continue
        try:
            start_date = datetime.strptime(timing[3] + " " + query["departure_date"][:4], "%A, %B %d %Y").date()
            if start_date.isoformat() != query["departure_date"]:
                continue
            end_date = datetime.strptime(timing[6] + " " + str(start_date.year), "%A, %B %d %Y").date()
            if end_date < start_date and start_date.month == 12 and end_date.month == 1:
                end_date = end_date.replace(year=start_date.year + 1)
            departure_time = datetime.strptime(timing[2], "%I:%M %p").strftime("%H:%M")
            arrival_time = datetime.strptime(timing[5], "%I:%M %p").strftime("%H:%M")
        except ValueError:
            continue
        hours = re.search(r"(\d+) hr", timing[7])
        minutes = re.search(r"(\d+) min", timing[7])
        duration = (int(hours[1]) * 60 if hours else 0) + (int(minutes[1]) if minutes else 0)
        if not 1 <= duration <= 10080 or not 0 <= (end_date - start_date).days <= 4:
            continue
        quote = re.search(r"^From ([\d,]+(?:\.\d+)?) Chinese yuan\.", label)
        price = float(quote[1].replace(",", "")) if quote else None
        stops = 0 if carrier[1] == "Nonstop" else int(carrier[1].split()[0])
        identity = (timing[1], timing[4], departure_time, arrival_time, carrier[2], end_date.isoformat(), stops, duration, price)
        if identity in seen:
            continue
        seen.add(identity)
        offers.append({"kind": "flight", "id": "flight-" + str(len(offers) + 1), "service": carrier[2], "airline": carrier[2],
                       "origin": timing[1], "destination": timing[4], "origin_code": airport_code(timing[1]),
                       "destination_code": airport_code(timing[4]), "departure_date": start_date.isoformat(), "departure_time": departure_time,
                       "arrival_date": end_date.isoformat(), "arrival_time": arrival_time, "duration_minutes": duration,
                       "stops": stops, "nonstop": stops == 0, "price_cny": price, "currency": "CNY",
                       "price_kind": "public_from_price", "passengers": 1, "cabin": "economy", "available": None,
                       "available_count": None, "checked_baggage": None, "refundable": None,
                       "source": source, "booking_url": source, "observed_at": observed})
    if not offers:
        raise TravelError("flight_response_unavailable", "未能读取明确的航班报价；可能是页面变化、暂未返回数据或无可用报价，不会伪称无票。")
    return offers


class FlightProvider:
    def __init__(self, http: ProviderHTTP):
        self.http = http

    async def search(self, query: dict) -> tuple[list[dict], dict]:
        source = flight_search_url(query)
        raw, final = await self.http.fetch(source, provider="flight", limit=8 * 1024 * 1024)
        offers = parse_flight_labels(raw, query, source)
        for field, key in (("origin", "origin_code"), ("destination", "destination_code")):
            offers = [offer for offer in offers if flight_endpoint_matches(query[field], offer[field], offer[key])]
        if not offers:
            raise TravelError("flight_airport_unverified", "网页返回的起终机场/城市与请求不一致或无法核验，不展示为匹配航班；请使用明确机场名称或三字码。")
        return offers, {"provider": "Google Flights 公开页面", "source": source, "observed_at": offers[0]["observed_at"],
                        "notices": ["单程、1 名成人、经济舱；价格为页面起价，不是锁价或支付总价承诺。",
                                    "无价格、行李、退改或余票数量信息的项目会明确标为待核实，不会当作满足硬条件。",
                                    "只读查询，不登录票务账户、不下单、不抢票；最终以航司或出票方为准。"]}

"""同程旅行国际机票 adapter（flight.ly.com）。

和携程不一样，同程的列表页是**服务端渲染**的 HTML：一个带 ``?date=`` 的 GET 就能
直接拿到全部航班卡片（广州→东京 81 条），不需要浏览器、不需要 cookie、没有验证码。
每张卡片的 ``<a href="https://m.ly.com/tiflight/book15.html?para=...">`` 就是真实下单页。

页面结构（``article.iflight-card``）::

    15:10  白云 T2     3h50m  ✈  直飞     20:00  羽田机场 T3
    南方航空 CZ3085   经济舱
    票面 ¥1970 + 税费 ¥447            ¥2417

中转航班多一个 ``<details class="seg-detail">``，每段有航班号、起降时刻和机场。

价格是「含税总价」，币种人民币。
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

import requests
from selectolax.lexbor import LexborHTMLParser

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.flight_time import TZ_CN, airport_timezone
from core.registry import CITIES, resolve_city
from core.schema import Money, Segment, TripOffer

BASE_URL = "https://flight.ly.com/travel/iflight/{o}-{d}?date={date}"
DETAIL_RE = re.compile(r"para=([A-Z]{3})\*([A-Z]{3})\*([\d-]+)")
DUR_RE = re.compile(r"(?:(\d+)h)?\s*(?:(\d+)m)?")
TRANSFER_RE = re.compile(r"中转\s*(\d+)\s*次")
CROSS_DAY = {"次日": 1, "2天后": 2, "第三天": 2}
TIMES = r"(\d{1,2}):(\d{2})"

# 中文城市/机场名 -> IATA 城市码（比价时统一按城市对齐）
_CITY_BY_NAME = {name: info["code"] for name, info in CITIES.items() if info.get("code")}

# cities.json 只收了我们常飞的城市；中转站会冒出别的城市，补一张常见中转点表。
# 这里给机场码（不是城市码），因为 flight_google 的时区表是按机场码建的。
_EXTRA_PLACES = {
    "首尔": "ICN", "仁川": "ICN", "釜山": "PUS", "济州": "CJU",
    "香港": "HKG", "澳门": "MFM", "台北": "TPE", "高雄": "KHH",
    "厦门": "XMN", "福州": "FOC", "青岛": "TAO", "大连": "DLC", "沈阳": "SHE",
    "天津": "TSN", "郑州": "CGO", "长沙": "CSX", "济南": "TNA", "合肥": "HFE",
    "南昌": "KHN", "贵阳": "KWE", "哈尔滨": "HRB", "长春": "CGQ", "石家庄": "SJW",
    "太原": "TYN", "兰州": "LHW", "乌鲁木齐": "URC", "呼和浩特": "HET",
    "宁波": "NGB", "温州": "WNZ", "珠海": "ZUH", "桂林": "KWL", "三亚": "SYX",
    "海口": "HAK", "吉隆坡": "KUL", "马尼拉": "MNL", "雅加达": "CGK",
    "巴厘岛": "DPS", "普吉": "HKT", "清迈": "CNX", "仰光": "RGN",
    "金边": "PNH", "万象": "VTE", "台北桃园": "TPE",
    # 中东 / 欧洲中转站（胡志明市 -> 伦敦这类洲际航线会出现）
    "迪拜": "DXB", "阿布扎比": "AUH", "多哈": "DOH", "利雅得": "RUH",
    "伊斯坦布尔": "IST", "阿什哈巴德": "ASB", "塔什干": "TAS",
    "巴黎": "CDG", "戴高乐": "CDG", "阿姆斯特丹": "AMS", "法兰克福": "FRA",
    "慕尼黑": "MUC", "苏黎世": "ZRH", "维也纳": "VIE", "罗马": "FCO",
    "米兰": "MXP", "马德里": "MAD", "巴塞罗那": "BCN", "布鲁塞尔": "BRU",
    "哥本哈根": "CPH", "斯德哥尔摩": "ARN", "赫尔辛基": "HEL", "华沙": "WAW",
    "纽约": "JFK", "洛杉矶": "LAX", "旧金山": "SFO", "芝加哥": "ORD",
    "多伦多": "YYZ", "温哥华": "YVR",
    "伦敦希思罗": "LHR", "希思罗": "LHR", "盖特威克": "LGW",
    "曼彻斯特": "MAN", "爱丁堡": "EDI", "伯明翰": "BHX",
}


def _money(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def _minutes(text: Optional[str]) -> int:
    if not text:
        return 0
    m = re.match(r"(?:(\d+)h)?\s*(?:(\d+)m)?", text.strip())
    if not m:
        return 0
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def _clock(text: Optional[str]) -> Optional[tuple[int, int]]:
    if not text:
        return None
    m = re.search(TIMES, text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _place_code(place: str) -> str:
    """'广州白云 T2' -> CAN；'东京成田 T1' -> TYO；认不出来的保留原名。"""
    cleaned = re.sub(r"\s*T\d+$", "", (place or "").strip())
    for name, code in _CITY_BY_NAME.items():
        if cleaned.startswith(name):
            return code
    for name, code in _EXTRA_PLACES.items():
        if cleaned.startswith(name):
            return code
    return cleaned or "?"


def _airline_and_numbers(text: str) -> tuple[str, str]:
    """'吉祥航空/上海航空 HO1852+FM815' -> ('吉祥航空/上海航空', 'HO1852+FM815')。"""
    parts = (text or "").rsplit(" ", 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z0-9+]+", parts[1]):
        return parts[0], parts[1]
    return (text or "").strip(), ""


def _parse_segments(card, dep_code: str, arr_code: str, date: str) -> list[Segment]:
    """优先用中转航段明细（有航班号/每段时刻），否则用卡片上的起降时刻拼一段。"""
    seg_items = card.css(".seg-item")
    names = _airline_and_numbers(_text(card, ".airline") or "")
    if seg_items:
        segments: list[Segment] = []
        cursor = datetime.fromisoformat(f"{date}T00:00:00")
        for i, li in enumerate(seg_items):
            flight = _text(li, ".seg-flight") or ""
            operator, numbers = _airline_and_numbers(flight)
            points = li.css(".seg-point")
            if len(points) < 2:
                continue
            dep_c = _clock(points[0].css_first(".seg-time").text(strip=True))
            arr_c = _clock(points[-1].css_first(".seg-time").text(strip=True))
            dep_place = points[0].css_first(".seg-place").text(strip=True)
            arr_place = points[-1].css_first(".seg-place").text(strip=True)
            if not dep_c or not arr_c:
                continue
            dep_dt = cursor.replace(hour=dep_c[0], minute=dep_c[1])
            # 上一段落地后要等下一段起飞；跨过零点就把日期往后推
            while dep_dt < cursor:
                dep_dt += timedelta(days=1)
            arr_dt = dep_dt.replace(hour=arr_c[0], minute=arr_c[1])
            if arr_dt <= dep_dt:
                arr_dt += timedelta(days=1)
            cursor = arr_dt
            dep_ap = dep_code if i == 0 else _place_code(dep_place)
            arr_ap = arr_code if i == len(seg_items) - 1 else _place_code(arr_place)
            segments.append(Segment(
                mode="flight",
                trip_no=numbers.split("+")[i] if "+" in numbers and i < len(numbers.split("+"))
                else numbers,
                operator=operator or names[0],
                origin_code=dep_ap,
                origin_name=dep_place,
                destination_code=arr_ap,
                destination_name=arr_place,
                depart_at=dep_dt.replace(tzinfo=airport_timezone(dep_ap)).isoformat(),
                arrive_at=arr_dt.replace(tzinfo=airport_timezone(arr_ap)).isoformat(),
                duration_min=_minutes(_text(li, ".seg-dur")),
            ))
        if segments:
            _apply_transfer(segments, card)
            return segments

    # 直飞（或没有明细）：卡片顶部两个时刻
    times = [e.text(strip=True) for e in card.css(".time-col .time")]
    if len(times) < 2:
        return []
    dep_c, arr_c = _clock(times[0]), _clock(times[1])
    if not dep_c or not arr_c:
        return []
    start = datetime.fromisoformat(f"{date}T00:00:00")
    dep_dt = start.replace(hour=dep_c[0], minute=dep_c[1])
    arr_dt = dep_dt.replace(hour=arr_c[0], minute=arr_c[1])
    cross = _text(card, ".cross-day")
    days = CROSS_DAY.get(cross or "", 0)
    if days:
        arr_dt += timedelta(days=days)
    elif arr_dt <= dep_dt:
        arr_dt += timedelta(days=1)
    seg = Segment(
        mode="flight",
        trip_no=names[1],
        operator=names[0],
        origin_code=dep_code,
        origin_name=_first_airport(card) or dep_code,
        destination_code=arr_code,
        destination_name=_last_airport(card) or arr_code,
        depart_at=dep_dt.replace(tzinfo=airport_timezone(dep_code)).isoformat(),
        arrive_at=arr_dt.replace(tzinfo=airport_timezone(arr_code)).isoformat(),
        duration_min=_minutes(_text(card, ".duration")),
    )
    _apply_transfer([seg], card)
    return [seg]


def _trips(text: str) -> int:
    m = TRANSFER_RE.search(text or "")
    return int(m.group(1)) if m else 0


def _apply_transfer(segments: list[Segment], card) -> None:
    """中转次数和经停城市挂在第一段上（CLI 只读第一段）。"""
    if not segments:
        return
    text = _text(card, ".transfer") or ""
    n = _trips(text)
    if not n:
        return
    via = []
    if "·" in text:
        via = [_place_code(v) for v in text.split("·", 1)[1].split("·") if v.strip()]
    segments[0].stops = n
    segments[0].via = via


def _text(node, selector: str) -> Optional[str]:
    el = node.css_first(selector)
    return el.text(strip=True) if el else None


def _first_airport(card) -> Optional[str]:
    cols = card.css(".time-col")
    if not cols:
        return None
    el = cols[0].css_first(".airport")
    return el.text(strip=True) if el else None


def _last_airport(card) -> Optional[str]:
    cols = card.css(".time-col")
    if len(cols) < 2:
        return None
    el = cols[-1].css_first(".airport")
    return el.text(strip=True) if el else None


def _booking_url(card) -> Optional[str]:
    a = card.css_first('a[href*="book15"]')
    if not a:
        return None
    href = a.attributes.get("href") or ""
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return "https://m.ly.com" + href
    return href


def parse_page(html: str, origin_code: str, destination_code: str,
               query: SearchQuery, fetched_at: str) -> list[TripOffer]:
    parser = LexborHTMLParser(html)
    cards = parser.css("article.iflight-card")
    if not cards:
        return []

    offers: list[TripOffer] = []
    for card in cards:
        airline_text = _text(card, ".airline") or ""
        if not airline_text:
            continue
        segments = _parse_segments(card, origin_code, destination_code, query.date)
        total = _money(_text(card, "em.price"))
        if not segments or not total:
            continue

        tax_text = _text(card, ".price-tax") or ""
        base = None
        tax = None
        m = re.search(r"票面\s*¥\s*([\d,]+)\s*\+\s*税费\s*¥\s*([\d,]+)", tax_text)
        if m:
            base = float(m.group(1).replace(",", ""))
            tax = float(m.group(2).replace(",", ""))
        cabin = _text(card, ".cabin") or query.cabin or "经济舱"
        numbers = "+".join(s.trip_no for s in segments if s.trip_no)
        key = (f"flight:{segments[0].origin_code}-{segments[-1].destination_code}:"
               f"{numbers or segments[0].operator}:"
               f"{segments[0].depart_at}:{segments[-1].arrive_at}")

        offers.append(TripOffer(
            offer_id=f"ly:{origin_code}-{destination_code}:{query.date}:{numbers or airline_text}",
            mode="flight",
            source="ly",
            source_name="同程旅行",
            segments=segments,
            price=Money(amount=total, currency="CNY", amount_cny=to_cny(total, "CNY"),
                        base=base, tax=tax),
            fare_class=cabin,
            price_note=(f"{cabin}含税总价（同程旅行"
                        + (f"，票面 ¥{base:.0f} + 税费 ¥{tax:.0f}" if base is not None else "")
                        + "）"),
            booking_url=_booking_url(card),
            fetched_at=fetched_at,
            seller="同程旅行",
            seller_type="ota",
            itinerary_key=key,
            raw={"airline": airline_text, "duration_text": _text(card, ".duration")},
        ))
    return offers


class FlightLyAdapter(BaseAdapter):
    """同程旅行国际机票（服务端渲染 HTML，无需浏览器）。"""

    source = "ly"
    source_name = "同程旅行"
    supported_modes = {"flight"}

    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            "accept-language": "zh-CN,zh;q=0.9",
        })

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        airports_o = origin.get("airports") or []
        airports_d = destination.get("airports") or []
        if not airports_o or not airports_d:
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")
        from_code = origin.get("code") or airports_o[0]
        to_code = destination.get("code") or airports_d[0]
        url = BASE_URL.format(o=from_code, d=to_code, date=query.date)

        last_err = ""
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                resp.encoding = "utf-8"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                continue

            html = resp.text
            if "个航班" not in html:
                last_err = "同程页面没有航班列表（可能该航线无数据或被限流）"
                continue

            fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
            offers = parse_page(html, from_code, to_code, query, fetched_at)
            if offers:
                return offers
            last_err = "同程返回了页面，但没解析出任何航班"
        raise AdapterError(last_err or "同程查询失败")

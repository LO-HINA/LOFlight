"""Kiwi.com adapter：纯 HTTP GraphQL，不需要浏览器。

Kiwi 的两条老路都关了：

- ``api.skypicker.com/flights``（老的公开 GET 接口）→ 404
- ``api.tequila.kiwi.com/v2/search`` → 403「'apikey' header is required」，要申请 key

但网页自己用的 GraphQL 接口可以打，而且**只要 TLS 指纹是 Chrome 就行**——
``curl_cffi`` + ``impersonate="chrome"`` 冷启动即可，不需要任何 ``kw-*`` 签名头：

    POST https://api.skypicker.com/umbrella/v2/graphql?featureName=SearchOneWayItinerariesQuery
    {"query": "...", "variables": {...}}

``query`` 有 25KB，存在 ``data/kiwi_query.graphql``；``variables`` 里只有航线、
日期、人数、币种需要按查询生成。

航线要用 Kiwi 自己的城市 id（``City:ho-chi-minh-city_vn``），先查::

    GET https://api.skypicker.com/locations?term=SGN&locale=en

拿机场对应的城市 id。

响应结构::

    data.onewayItineraries.itineraries[]
      price.amount                        总价（币种由 options.currency 决定）
      duration                            总耗时（秒）
      bagsInfo.includedCheckedBags        含托运件数
      sector.sectorSegments[].segment
        source/destination.station.code   机场三字码
        source/destination.localTime      当地墙钟时间
        source/destination.utcTimeIso     UTC 时间（用它反推时区偏移，不用查表）
        carrier.code + code               航班号（MU + 6024）

时区直接由 ``localTime - utcTimeIso`` 算出来，不依赖本地时区表——Kiwi 覆盖全球，
任何小机场都能算对。
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Optional

from curl_cffi import requests as curl_requests

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.flight_time import TZ_CN
from core.registry import resolve_city
from core.schema import Baggage, Money, Segment, TripOffer

LOCATIONS_URL = "https://api.skypicker.com/locations"
GRAPHQL_URL = ("https://api.skypicker.com/umbrella/v2/graphql"
               "?featureName=SearchOneWayItinerariesQuery")
QUERY_PATH = Path(__file__).resolve().parents[2] / "data" / "kiwi_query.graphql"
BASE_HEADERS = {
    "content-type": "application/json",
    "accept": "application/json, text/plain, */*",
    "origin": "https://www.kiwi.com",
    "referer": "https://www.kiwi.com/",
}
CABIN = {"经济舱": "ECONOMY", "超级经济舱": "PREMIUM_ECONOMY",
         "商务舱": "BUSINESS", "头等舱": "FIRST"}

_QUERY: Optional[str] = None


def _query_text() -> str:
    global _QUERY
    if _QUERY is None:
        _QUERY = QUERY_PATH.read_text(encoding="utf-8")
    return _QUERY


def _variables(from_id: str, to_id: str, date: str, cabin: str,
               limit: int = 100, currency: str = "cny") -> dict:
    """模板照抄网页请求，只换航线/日期/舱位/条数。"""
    return {
        "search": {
            "itinerary": {
                "source": {"ids": [from_id]},
                "destination": {"ids": [to_id]},
                "outboundDepartureDate": {"start": f"{date}T00:00:00",
                                          "end": f"{date}T23:59:59"},
            },
            "passengers": {"adults": 1, "children": 0, "infants": 0,
                           "adultsHoldBags": [0], "adultsHandBags": [0],
                           "childrenHoldBags": [], "childrenHandBags": []},
            "cabinClass": {"cabinClass": CABIN.get(cabin, "ECONOMY"),
                           "applyMixedClasses": False},
        },
        "filter": {
            "allowChangeInboundDestination": True,
            "allowChangeInboundSource": True,
            "allowDifferentStationConnection": True,
            "enableSelfTransfer": True,
            "enableThrowAwayTicketing": True,
            "enableTrueHiddenCity": True,
            "transportTypes": ["FLIGHT"],
            "contentProviders": ["KIWI", "FRESH"],
            "flightsApiLimit": limit * 2,
            "limit": limit,
        },
        "options": {
            "sortBy": "PRICE",
            "contentProviders": ["KIWI", "FRESH"],
            "currency": currency,
            "apiUrl": None,
            "locale": "en",
            "market": "us",
            "partner": "skypicker",
            "partnerMarket": "us",
            "affilID": "skypicker",
            "storeSearch": False,
            "kiwiClub": {"isKiwiClubMember": False, "isPhoneVerified": False,
                         "kiwiClubPerksEligible": False},
            "abTestInput": {"airLuggageTest": "ENABLE", "benefitsSunset": "ENABLE",
                            "top5ResultsDiversity": "ENABLE"},
            "serverToken": None,
            "searchSessionId": None,
        },
        "includeItineraryDebug": False,
    }


def _place(session, city: dict) -> tuple[str, str]:
    """城市名/机场码 -> (``City:xxx``, slug)。slug 用来拼下单页地址。"""
    candidates = []
    for ap in (city.get("airports") or [])[:2]:
        candidates.append(ap.lower())
    for alias in city.get("aliases") or []:
        if alias.isascii():
            candidates.append(alias)
    candidates.append(city.get("name") or "")

    for term in candidates:
        if not term:
            continue
        try:
            resp = session.get(LOCATIONS_URL, params={"term": term, "locale": "en",
                                                      "limit": 10},
                               headers=BASE_HEADERS, timeout=30)
            locations = (resp.json() or {}).get("locations") or []
        except Exception:
            continue
        # 机场条目自带 city 字段，比按名字搜更准
        for loc in locations:
            if loc.get("type") == "airport" and str(loc.get("id", "")).lower() == term:
                inner = loc.get("city") or {}
                if inner.get("id"):
                    return (inner["id"] if inner["id"].startswith("City:")
                            else f"City:{inner['id']}", inner.get("slug") or "")
        for loc in locations:
            if loc.get("type") == "city" and loc.get("id"):
                if not loc.get("country") or loc.get("country", {}).get("code") == city.get("country"):
                    return f"City:{loc['id']}", loc.get("slug") or ""
        for loc in locations:
            if loc.get("type") == "city" and loc.get("id"):
                return f"City:{loc['id']}", loc.get("slug") or ""
    raise AdapterError(f"Kiwi 找不到城市：{city.get('name')}")


def _iso(point: dict) -> str:
    """localTime 是墙钟，utcTimeIso 是 UTC——两者相减就是该机场的时区偏移。

    这样不用维护时区表，任何小机场都算得对。
    """
    local = datetime.fromisoformat(point["localTime"])
    utc = datetime.fromisoformat(point["utcTimeIso"].replace("Z", "+00:00"))
    offset = local - utc.replace(tzinfo=None)
    return local.replace(tzinfo=timezone(offset)).isoformat()


def _baggage(itinerary: dict) -> Optional[Baggage]:
    info = itinerary.get("bagsInfo") or {}
    checked = info.get("includedCheckedBags")
    hand = info.get("includedHandBags")
    if checked is None and hand is None:
        return None
    return Baggage(
        carry_on=f"手提 {hand} 件" if hand is not None else None,
        checked=f"托运 {checked} 件" if checked else "无免费托运",
        pieces=checked or None,
    )


def parse_itinerary(itinerary: dict, query: SearchQuery, booking_url: str,
                    fetched_at: str) -> Optional[TripOffer]:
    segments: list[Segment] = []
    for item in (itinerary.get("sector") or {}).get("sectorSegments") or []:
        seg = item.get("segment") or {}
        src, dst = seg.get("source") or {}, seg.get("destination") or {}
        carrier = seg.get("carrier") or {}
        if not src.get("station") or not dst.get("station"):
            continue
        segments.append(Segment(
            mode="flight",
            trip_no=f"{carrier.get('code', '')}{seg.get('code', '')}",
            operator=carrier.get("name") or carrier.get("code") or "",
            origin_code=src["station"].get("code") or "",
            origin_name=src["station"].get("name") or "",
            destination_code=dst["station"].get("code") or "",
            destination_name=dst["station"].get("name") or "",
            depart_at=_iso(src),
            arrive_at=_iso(dst),
            duration_min=int((seg.get("duration") or 0) / 60),
        ))
    if not segments:
        return None

    price_text = (itinerary.get("price") or {}).get("amount")
    try:
        amount = float(price_text)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    if len(segments) > 1:
        segments[0].stops = len(segments) - 1
        segments[0].via = [s.origin_code for s in segments[1:]]

    numbers = "+".join(s.trip_no for s in segments)
    key = (f"flight:{segments[0].origin_code}-{segments[-1].destination_code}:"
           f"{numbers}:{segments[0].depart_at}:{segments[-1].arrive_at}")
    cabin = query.cabin or "经济舱"
    provider = (itinerary.get("provider") or {}).get("name") or "Kiwi.com"

    return TripOffer(
        offer_id=f"kiwi:{itinerary.get('shareId') or itinerary.get('id') or numbers}",
        mode="flight",
        source="kiwi",
        source_name="Kiwi.com",
        segments=segments,
        price=Money(amount=amount, currency="CNY", amount_cny=to_cny(amount, "CNY")),
        fare_class=cabin,
        price_note=f"{cabin}含税总价（Kiwi.com，供应商 {provider}）",
        baggage=_baggage(itinerary),
        booking_url=booking_url,
        fetched_at=fetched_at,
        seller="Kiwi.com",
        seller_type="ota",
        itinerary_key=key,
        raw={"provider": provider},
    )


class FlightKiwiAdapter(BaseAdapter):
    """Kiwi.com：纯 HTTP GraphQL，不弹窗、不需要 key。"""

    source = "kiwi"
    source_name = "Kiwi.com"
    supported_modes = {"flight"}

    def __init__(self, timeout: float = 90.0, limit: int = 100):
        self.timeout = timeout
        self.limit = limit

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        if not origin.get("airports") or not destination.get("airports"):
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")

        with curl_requests.Session(impersonate="chrome") as session:
            from_id, from_slug = _place(session, origin)
            to_id, to_slug = _place(session, destination)
            variables = _variables(from_id, to_id, query.date,
                                   query.cabin or "经济舱", self.limit)
            body = json.dumps({"query": _query_text(), "variables": variables})
            try:
                resp = session.post(GRAPHQL_URL, data=body.encode(),
                                    headers=BASE_HEADERS, timeout=self.timeout)
            except Exception as e:
                raise AdapterError(f"Kiwi 请求失败：{type(e).__name__}: {e}")

        if resp.status_code != 200:
            raise AdapterError(f"Kiwi HTTP {resp.status_code}：{resp.text[:120]}")
        try:
            data = resp.json()
        except Exception:
            raise AdapterError(f"Kiwi 返回的不是 JSON：{resp.text[:120]}")

        block = (data.get("data") or {}).get("onewayItineraries") or {}
        itineraries = block.get("itineraries") or []
        if not itineraries:
            errors = block.get("errors") or (data.get("errors")) or []
            raise AdapterError(f"Kiwi 没返回行程：{str(errors)[:200] or resp.text[:160]}")

        booking_url = (f"https://www.kiwi.com/en/search/results/{from_slug}/{to_slug}/"
                       f"{query.date}/no-return?currency=cny")
        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        offers: list[TripOffer] = []
        for it in itineraries:
            try:
                offer = parse_itinerary(it, query, booking_url, fetched_at)
            except Exception:
                continue
            if offer:
                offers.append(offer)
        if not offers:
            raise AdapterError("Kiwi 返回了行程，但都没解析出价格")
        return offers

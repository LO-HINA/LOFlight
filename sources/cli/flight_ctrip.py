"""携程机票 adapter：真 Chrome + CDP 打开列表页，解析 batchSearch 的 JSON。

为什么不直接 requests：携程给 SPA 前面挂了 whaleguard，Playwright 自带启动会被识别
（页面正文只剩 17 个字符）。用本机 Chrome 开远程调试端口再 CDP 连进去，就和使用
真人浏览器一样，能正常拿到 3MB 的搜索结果。

数据接口（页面自动调用，我们只监听不修改）：
    POST https://flights.ctrip.com/international/search/api/search/batchSearch
    → data.flightItineraryList[]  每个行程的价格档、航班号、起降时刻、行李额

一次搜索会分多批返回（``context.finished`` 从 false 变 true），所以把每次响应的
``flightItineraryList`` 都收下来，按 ``itineraryId`` 合并，后到的覆盖先到的
（滚动时携程会重新定价）。

币种：国内版页面固定人民币，响应里没有 currency 字段，这里按 CNY 处理。
"""
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.browser import real_chrome
from core.currency import to_cny
from core.flight_time import AIRPORT_TZ_HOURS, COUNTRY_TZ_HOURS, TZ_CN, airport_timezone
from core.registry import resolve_city
from core.schema import Baggage, Money, Segment, TripOffer

CABIN_CN = {"Y": "经济舱", "W": "超级经济舱", "C": "商务舱", "J": "商务舱", "F": "头等舱"}
SEARCH_URL = ("https://flights.ctrip.com/online/list/oneway-{o}-{d}?depdate={date}")


def _leg_tz(leg: dict, code: str, side: str) -> timezone:
    """机场时区；没见过的新机场退回该段国家。

    携程会返回没收录的代码（如 NSZ 南沙港），不知道时区时按国家兜底，
    否则会算成 +00:00，耗时全错。
    """
    if code and (code in AIRPORT_TZ_HOURS):
        return airport_timezone(code)
    country = leg.get(f"{side}CountryCode")
    return timezone(timedelta(hours=COUNTRY_TZ_HOURS.get(country, 8)))


def _price_total(p: dict) -> float:
    return float(p.get("adultPrice") or 0) + float(p.get("adultTax") or 0)


def _pick_price(prices: list[dict]) -> Optional[dict]:
    """同一行程携程会给 1-4 个运价档，取最便宜的那档做比价基准。"""
    usable = [p for p in prices or [] if _price_total(p) > 0]
    return min(usable, key=_price_total) if usable else None


def _baggage(price: dict) -> Optional[Baggage]:
    bags = (price.get("baggage") or {}).get("dataList") or []
    if not bags:
        return None
    adult = (bags[0] or {}).get("adultBaggage") or {}
    checked = (adult.get("checkedBaggage") or {}).get("baggageContent")
    carry = (adult.get("handBaggage") or {}).get("baggageContent")
    if not checked and not carry:
        return None
    return Baggage(carry_on=carry, checked=checked,
                   note=(adult.get("checkedBaggage") or {}).get("baggageSizeLimit"))


def _legs(itinerary: dict) -> list[dict]:
    out: list[dict] = []
    for seg in itinerary.get("flightSegments") or []:
        out.extend(seg.get("flightList") or [])
    return out


def _is_pure_flight(itinerary: dict) -> bool:
    """携程会混进「码头 → 香港机场 → 航班」的海陆联运（trafficType=SEAPLANE）。

    这些不是航班报价，和 Google 那边也没法比价，直接丢掉。
    """
    legs = _legs(itinerary)
    return bool(legs) and all((l.get("trafficType") or "FLIGHT") == "FLIGHT" for l in legs)


def _segments(itinerary: dict) -> list[Segment]:
    legs = _legs(itinerary)
    segments: list[Segment] = []
    for i, leg in enumerate(legs):
        dep_ap = leg.get("departureAirportCode", "")
        arr_ap = leg.get("arrivalAirportCode", "")
        stops = int(leg.get("stopCount") or 0)
        via: list[str] = []
        # 中转信息挂在第一段上（CLI 只用第一段画「经停/中转」）
        if i == 0 and len(legs) > 1:
            stops = len(legs) - 1
            via = [l.get("departureAirportCode", "") for l in legs[1:]]
        segments.append(Segment(
            mode="flight",
            trip_no=leg.get("flightNo") or "",
            operator=leg.get("marketAirlineName") or leg.get("airlineName") or "",
            origin_code=dep_ap,
            origin_name=f"{leg.get('departureCityName', '')}{leg.get('departureAirportShortName', '')}",
            destination_code=arr_ap,
            destination_name=f"{leg.get('arrivalCityName', '')}{leg.get('arrivalAirportShortName', '')}",
            depart_at=datetime.fromisoformat(leg["departureDateTime"]).replace(
                tzinfo=_leg_tz(leg, dep_ap, "departure")).isoformat(),
            arrive_at=datetime.fromisoformat(leg["arrivalDateTime"]).replace(
                tzinfo=_leg_tz(leg, arr_ap, "arrival")).isoformat(),
            duration_min=int(leg.get("duration") or 0),
            stops=stops,
            via=via,
        ))
    return segments


def parse_itinerary(itinerary: dict, query: SearchQuery, booking_url: str,
                    fetched_at: str) -> Optional[TripOffer]:
    if not _is_pure_flight(itinerary):
        return None
    segments = _segments(itinerary)
    price = _pick_price(itinerary.get("priceList") or [])
    if not segments or not price:
        return None

    total = _price_total(price)
    cabin_code = price.get("cabin") or "Y"
    cabin = CABIN_CN.get(cabin_code, query.cabin or "经济舱")
    brands = []
    for tag in price.get("priceTags") or []:
        if isinstance(tag, dict):
            detail = tag.get("data")
            brand_name = detail.get("brandName") if isinstance(detail, dict) else None
            if brand_name:
                brands.append(brand_name)
            elif tag.get("label"):
                brands.append(tag["label"])
    brand = next((b for b in brands if b), "")
    numbers = "+".join(s.trip_no for s in segments)
    key = (f"flight:{segments[0].origin_code}-{segments[-1].destination_code}:"
           f"{numbers}:{segments[0].depart_at}:{segments[-1].arrive_at}")

    return TripOffer(
        offer_id=f"ctrip:{itinerary.get('itineraryId') or key}",
        mode="flight",
        source="ctrip",
        source_name="携程",
        segments=segments,
        price=Money(
            amount=total, currency="CNY", amount_cny=to_cny(total, "CNY"),
            base=float(price.get("adultPrice") or 0),
            tax=float(price.get("adultTax") or 0),
        ),
        fare_class=cabin,
        price_note=(f"{cabin}含税总价（携程，舱位 {cabin_code}"
                    + (f"，{brand}" if brand else "") + "）"),
        baggage=_baggage(price),
        booking_url=booking_url,
        fetched_at=fetched_at,
        seller="携程",
        seller_type="ota",
        itinerary_key=key,
        raw={"itinerary_id": itinerary.get("itineraryId"),
             "channel": price.get("channel"),
             "price_options": len(itinerary.get("priceList") or []),
             "transfer_count": (itinerary.get("flightSegments") or [{}])[0].get("transferCount")},
    )


def merge_results(batches: list[list[dict]]) -> list[dict]:
    """多批 batchSearch 结果按 itineraryId 合并，后到的覆盖先到的（重新定价）。"""
    merged: dict[str, dict] = {}
    for batch in batches:
        for it in batch:
            iid = it.get("itineraryId")
            if iid:
                merged[iid] = it
    return list(merged.values())


class FlightCtripAdapter(BaseAdapter):
    source = "ctrip"
    source_name = "携程"
    supported_modes = {"flight"}

    def __init__(self, timeout: float = 45.0, scrolls: int = 3):
        self.timeout = timeout
        self.scrolls = scrolls

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        airports_o = origin.get("airports") or []
        airports_d = destination.get("airports") or []
        if not airports_o or not airports_d:
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")
        # 城市码（TYO / BJS / SHA）能一次覆盖该城所有机场，和 Google 那边对齐
        from_code = origin.get("code") or airports_o[0]
        to_code = destination.get("code") or airports_d[0]
        url = SEARCH_URL.format(o=from_code.lower(), d=to_code.lower(), date=query.date)

        batches: list[list[dict]] = []
        finished_flags: list[bool] = []

        def on_response(resp) -> None:
            if "batchSearch" not in resp.url:
                return
            try:
                payload = resp.json()
            except Exception:
                return
            data = (payload or {}).get("data") or {}
            rows = data.get("flightItineraryList") or []
            finished_flags.append(bool((data.get("context") or {}).get("finished")))
            if rows:
                batches.append(rows)

        with real_chrome() as browser:
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                raise AdapterError(f"携程页面打不开：{type(e).__name__}: {e}")

            # 携程分多批返回（context.finished），等它报完或超时为止；
            # 中途滚动触发后续分页，顺便让价格重新校准一次。
            deadline = time.time() + self.timeout
            scrolls_left = self.scrolls
            while time.time() < deadline:
                page.wait_for_timeout(700)
                if finished_flags and finished_flags[-1]:
                    break
                if scrolls_left and batches:
                    page.mouse.wheel(0, 2600)
                    scrolls_left -= 1

        if not batches:
            raise AdapterError("携程没有返回 batchSearch 数据（可能被风控或当天无票）")

        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        offers: list[TripOffer] = []
        for it in merge_results(batches):
            try:
                offer = parse_itinerary(it, query, url, fetched_at)
            except Exception:
                continue
            if offer:
                offers.append(offer)
        if not offers:
            raise AdapterError("携程返回了数据，但没解析出任何带价格的行程")
        return offers

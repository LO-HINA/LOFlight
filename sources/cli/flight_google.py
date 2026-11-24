"""Google Flights 航班 adapter。

不用 fast_flights 自带的 parse（遇到无价行程会 IndexError），只借用它拼查询和抓 HTML，
解析由本文件完成，字段位置与 Google 的 ds:1 数据块对应。

已验证：HAN -> SGN 2026-12-01 返回 56 条；SHA -> PEK 国内航线也能解析。
限制：Google Flights 不返回航班号，trip_no 暂用航司二字码；行李/退改规则不返回。
"""
import json
from datetime import datetime
from itertools import product
from typing import Optional
from urllib.parse import quote

from fast_flights import FlightQuery, Passengers, create_query, fetch_flights_html
from selectolax.lexbor import LexborHTMLParser

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.flight_time import TZ_CN, airport_timezone
from core.registry import resolve_city
from core.schema import Money, Segment, TripOffer

SEAT_MAP = {"经济舱": "economy", "商务舱": "business", "头等舱": "first"}


def _parse_clock(value) -> tuple[int, int]:
    """Google 会省略末尾的 0： [8] = 08:00，[None,31] = 00:31。"""
    padded = [*(value or []), None, None]
    return (padded[0] or 0, padded[1] or 0)


def _iso(date_tuple, clock: tuple[int, int], airport_code: str) -> str:
    y, m, d = date_tuple[0], date_tuple[1], date_tuple[2]
    dt = datetime(y, m, d, clock[0], clock[1], tzinfo=airport_timezone(airport_code))
    return dt.isoformat()


def _extract_ds1(html: str) -> str:
    parser = LexborHTMLParser(html)
    best = ""
    for script in parser.css("script"):
        classes = (script.attributes.get("class", "") or "").split()
        if "ds:1" in classes:
            text = script.text() or ""
            if len(text) > len(best):
                best = text
    if not best:
        raise AdapterError("Google Flights 页面结构变化：没有找到 ds:1 数据块")
    return best


def _parse_payload(js: str) -> list[dict]:
    data = js.split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        raise AdapterError("Google Flights 没有返回航班")
    payload = json.loads(data)
    if not payload[3] or payload[3][0] is None:
        return []

    rows: list[dict] = []
    for item in payload[3][0]:
        try:
            flight, price = item[0], item[1][0][1]
            legs_raw = flight[2]
            if not price or not legs_raw:
                continue
        except (IndexError, TypeError):
            continue

        legs = []
        for leg in legs_raw:
            try:
                depart_clock = _parse_clock(leg[8])
                arrive_clock = _parse_clock(leg[10])
                depart_date = tuple(leg[20])
                arrive_date = tuple(leg[21])
                legs.append({
                    "from_code": leg[3],
                    "from_name": leg[4],
                    "to_code": leg[6],
                    "to_name": leg[5],
                    "depart_clock": depart_clock,
                    "arrive_clock": arrive_clock,
                    "depart_date": depart_date,
                    "arrive_date": arrive_date,
                    "duration": int(leg[11] or 0),
                    "plane": leg[17],
                })
            except (IndexError, TypeError, ValueError):
                continue
        if not legs:
            continue
        rows.append({
            "airline_code": flight[0],
            "airlines": flight[1] or [],
            "price": float(price),
            "legs": legs,
        })
    return rows


class FlightGoogleAdapter(BaseAdapter):
    source = "google_flights"
    source_name = "Google Flights"
    supported_modes = {"flight"}

    def __init__(self, max_combos: int = 4):
        self.max_combos = max_combos

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        from_airports = origin.get("airports") or []
        to_airports = destination.get("airports") or []
        if not from_airports or not to_airports:
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")

        combos = list(product(from_airports, to_airports))[: self.max_combos]
        seat = SEAT_MAP.get(query.cabin or "经济舱", "economy")
        offers: list[TripOffer] = []
        errors: list[str] = []

        for from_ap, to_ap in combos:
            try:
                offers.extend(self._query_pair(query, from_ap, to_ap, seat))
            except Exception as e:
                errors.append(f"{from_ap}->{to_ap}: {e}")

        if not offers and errors:
            raise AdapterError("；".join(errors))
        return offers

    def _query_pair(self, query: SearchQuery, from_ap: str, to_ap: str, seat: str) -> list[TripOffer]:
        q = create_query(
            flights=[FlightQuery(date=query.date, from_airport=from_ap, to_airport=to_ap)],
            seat=seat,
            trip="one-way",
            passengers=Passengers(adults=max(1, query.passengers)),
            currency="CNY",
            language="zh-CN",
        )
        rows = _parse_payload(_extract_ds1(fetch_flights_html(q)))
        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        booking_url = self._booking_url(from_ap, to_ap, query.date)
        cabin_name = query.cabin or "经济舱"
        offers: list[TripOffer] = []

        for row in rows:
            segments = []
            for leg in row["legs"]:
                segments.append(Segment(
                    mode="flight",
                    trip_no=row["airline_code"] or "?",
                    operator=" / ".join(row["airlines"]) or row["airline_code"] or "未知航司",
                    origin_code=leg["from_code"],
                    origin_name=leg["from_name"],
                    destination_code=leg["to_code"],
                    destination_name=leg["to_name"],
                    depart_at=_iso(leg["depart_date"], leg["depart_clock"], leg["from_code"]),
                    arrive_at=_iso(leg["arrive_date"], leg["arrive_clock"], leg["to_code"]),
                    duration_min=leg["duration"],
                    stops=0,
                ))
            if not segments:
                continue
            stops = len(segments) - 1
            via = [seg.destination_code for seg in segments[:-1]]
            for seg in segments:
                seg.stops = stops
                seg.via = via

            offers.append(TripOffer(
                offer_id=(
                    f"google_flights:{from_ap}-{to_ap}:{query.date}:"
                    f"{segments[0].depart_at[11:16]}:{row['airline_code']}:{int(row['price'])}"
                ),
                mode="flight",
                source=self.source,
                source_name=self.source_name,
                segments=segments,
                price=Money(
                    amount=row["price"],
                    currency="CNY",
                    amount_cny=to_cny(row["price"], "CNY"),
                ),
                fare_class=cabin_name,
                price_note=f"{cabin_name}，Google Flights 总价（含税）",
                booking_url=booking_url,
                fetched_at=fetched_at,
                raw={
                    "airline_code": row["airline_code"],
                    "airlines": row["airlines"],
                    "plane_types": [leg["plane"] for leg in row["legs"]],
                },
            ))
        return offers

    @staticmethod
    def _booking_url(from_ap: str, to_ap: str, date: str) -> str:
        q = quote(f"Flights from {from_ap} to {to_ap} on {date}")
        return f"https://www.google.com/travel/flights?hl=zh-CN&curr=CNY&q={q}"

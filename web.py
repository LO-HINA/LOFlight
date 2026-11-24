"""本地 HTML 与实时机票查询接口。"""
import logging
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore
from urllib.request import getproxies

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core.adapter import SearchQuery
from core.compare import build_itineraries
from core.flight_time import TZ_CN
from core.registry import CITIES, city_code_for, resolve_city
from sources.flight_ctrip_public import fetch_public_quotes
from sources.flight_ly import BASE_URL as LY_SEARCH_URL
from sources.flight_ly import FlightLyAdapter
from sources.flight_ly_domestic import fetch_domestic_quotes
from sources.flight_trip import SEARCH_URL as TRIP_SEARCH_URL
from sources.flight_trip import FlightTripAdapter

_PAGE = Path(__file__).resolve().parent / "prototype" / "index.html"
app = FastAPI()
app.mount("/assets", StaticFiles(directory=_PAGE.parent / "assets"), name="assets")
_LOG = logging.getLogger("uvicorn.error")
_SEARCH_SLOTS = BoundedSemaphore(3)
_CABIN = "经济舱"
_NUMBER = re.compile(r"[A-Z0-9]{2}\d{1,4}[A-Z]?")
_CITIES_BY_CODE = {
    code: name for name, city in CITIES.items()
    for code in {city["code"], *city["airports"]}
}


@app.get("/", include_in_schema=False)
def page():
    return FileResponse(_PAGE, headers={"Cache-Control": "no-store"})


@app.get("/api/cities")
def cities():
    return [{"name": name, **city} for name, city in CITIES.items()]


def _city(value):
    value = value.strip()
    try:
        return resolve_city(_CITIES_BY_CODE.get(value.upper(), value))
    except KeyError:
        raise HTTPException(422, f"暂未收录「{value}」，请从城市联想列表中选择。") from None


def _number(segment):
    return segment.trip_no.replace(" ", "").upper()


def _flight_key(offer):
    return tuple(
        (_number(segment), city_code_for(segment.origin_code), city_code_for(segment.destination_code),
         segment.depart_at[:16].replace(" ", "T"), segment.arrive_at[:16].replace(" ", "T"))
        for segment in offer.segments
    )


def _fare(offer, fallback_url):
    url = offer.booking_url or ""
    if offer.source == "ly" and url.startswith(("https://m.ly.com/", "https://flight.ly.com/")):
        note = "含税 · 平台行程页"
    else:
        url, note = fallback_url, "含税 · 平台搜索页"
    return {"seller": offer.seller or offer.source_name, "price": offer.price.amount_cny,
            "note": note, "url": url}


def _flight(group, search_urls):
    offer = next((row for row in group.offers if row.source == "trip"), group.cheapest)
    first, last = offer.segments[0], offer.segments[-1]
    start, end = datetime.fromisoformat(first.depart_at), datetime.fromisoformat(last.arrive_at)
    return {
        "numbers": [_number(segment) for segment in offer.segments],
        "airline": " / ".join(dict.fromkeys(segment.operator for segment in offer.segments)),
        "departure": start.strftime("%H:%M"),
        "arrival": end.strftime("%H:%M"),
        "arrivalDays": (end.date() - start.date()).days,
        "originAirport": first.origin_name or first.origin_code,
        "destinationAirport": last.destination_name or last.destination_code,
        "duration": offer.total_duration_min,
        "stops": max(len(offer.segments) - 1, first.stops),
        "via": " · ".join(first.via or [segment.destination_code for segment in offer.segments[:-1]]),
        "quotes": [_fare(row, search_urls[row.source]) for row in group.offers],
    }


def _result(name, job):
    try:
        return job.result()
    except Exception as error:
        _LOG.warning("%s 本次查询失败 (%s)", name, type(error).__name__)
        return None


def _reference_key(row):
    return tuple(row["numbers"]), row["departure"], row["arrival"]


def _search(origin, destination, departure):
    o, d, day = origin["code"], destination["code"], departure.isoformat()
    domestic = origin["country"] == destination["country"] == "CN"
    query = SearchQuery(origin["name"], destination["name"], day, mode="flight", cabin=_CABIN)
    search_urls = {
        "ly": LY_SEARCH_URL.format(o=o, d=d, date=day),
        "trip": TRIP_SEARCH_URL.format(o=o.lower(), d=d.lower(), date=day, cls="y"),
        "ctrip": (f"https://flights.ctrip.com/online/list/oneway-{o.lower()}-{d.lower()}"
                  f"?depdate={day}&cabin=y_s_c_f&adult=1&child=0&infant=0"),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        trip = pool.submit(FlightTripAdapter(use_browser=False, http_timeout=12).search, query)
        ly = (pool.submit(fetch_domestic_quotes, o, d, departure) if domestic else
              pool.submit(FlightLyAdapter(timeout=12, retries=1).search, query))
        public = None
        if departure >= datetime.now(TZ_CN).date():
            public = pool.submit(fetch_public_quotes, o, d, departure)
        offers, channels = [], []
        reference_quotes = []
        for name, job in (("Trip.com", trip), ("同程旅行", ly)):
            rows = _result(name, job)
            if domestic and job is ly:
                reference_quotes.extend(rows or [])
                status = "failed" if rows is None else "reference" if rows else "empty"
                channels.append({"name": name, "status": status})
                continue
            valid = [
                row for row in rows or []
                if row.fare_class == _CABIN and row.segments
                and len(row.segments) >= row.segments[0].stops + 1
                and all(_NUMBER.fullmatch(_number(segment)) for segment in row.segments)
                and row.segments[0].depart_at[:10] == day
                and math.isfinite(row.price.amount_cny) and row.price.amount_cny > 0
                and "含税" in row.price_note and "不含税" not in row.price_note
            ]
            offers.extend(valid)
            channels.append({"name": name, "status": "failed" if rows is None else "quoted" if valid else "empty"})
        ctrip = _result("携程", public) if public else None

    has_ctrip = False
    for row in ctrip["offers"] if ctrip else []:
        if set(row["cabin_classes"]) != {_CABIN}:
            continue
        has_ctrip = True
        reference_quotes.append({
            "seller": "携程", "price": row["price_from_cny"], "url": search_urls["ctrip"],
            "numbers": row["flight_numbers"], "departure": row["departure_time_local"],
            "arrival": row["arrival_time_local"],
        })
    status = "reference" if has_ctrip else "empty" if ctrip else "failed" if public else "unavailable"
    channels.append({"name": "携程", "status": status})

    references = {}
    for quote in sorted(reference_quotes, key=lambda row: row["price"]):
        sellers = references.setdefault(_reference_key(quote), {})
        sellers.setdefault(quote["seller"], quote)
    flights = [_flight(group, search_urls) for group in build_itineraries(offers, key_fn=_flight_key)]
    attached = set()
    for flight in flights:
        key = _reference_key(flight)
        if key in references:
            flight["references"] = list(references[key].values())
            if "携程" in references[key]:
                flight["reference"] = references[key]["携程"]
            attached.add(key)
    return {
        "origin": origin["name"], "destination": destination["name"], "date": day,
        "captured": datetime.now(timezone.utc).isoformat(), "flights": flights, "channels": channels,
        "references": sorted((quote for key, sellers in references.items() if key not in attached
                              for quote in sellers.values()),
                             key=lambda quote: quote["price"]),
    }


@app.get("/api/flights")
def flights(
    response: Response, departure: date,
    origin: str = Query(min_length=1, max_length=80),
    destination: str = Query(min_length=1, max_length=80),
):
    start, end = _city(origin), _city(destination)
    if start["name"] == end["name"]:
        raise HTTPException(422, "请选择不同的出发和到达城市。")
    if departure < date.today():
        raise HTTPException(422, "请选择今天或之后的出发日期。")
    if not _SEARCH_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "查价服务正忙，请稍等片刻再试。")
    try:
        response.headers["Cache-Control"] = "no-store"
        return _search(start, end, departure)
    finally:
        _SEARCH_SLOTS.release()


if __name__ == "__main__":
    # Give the HTTP adapters the same proxy defaults, including Windows system settings.
    for scheme, proxy in getproxies().items():
        if scheme in {"http", "https"}:
            os.environ.setdefault(scheme + "_proxy", proxy)
    uvicorn.run(app, host="127.0.0.1", port=8765)

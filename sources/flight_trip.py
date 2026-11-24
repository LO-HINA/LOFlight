"""Trip.com（携程国际版）机票 adapter。

两个通路，优先走第一条：

1. **纯 HTTP（默认）**：``curl_cffi`` 用 ``impersonate="chrome"`` 冒充 Chrome 的
   TLS/JA3 指纹，先 GET 搜索页拿 cookie，再 POST::

       https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE

   —— 一次返回 ``data:{...}``（SSE），里面 ``itineraryList`` 就是当天全部行程。
   实测 0.5–1.2MB、99–225 个行程。**不用浏览器、不用弹窗，手机上也能直接照做。**

   注意：必须用 ``curl_cffi``。普通 ``requests`` 的 TLS 指纹会被 whaleguard 判死，
   同一个请求直接返回 ``HTTP 430 whaleguard block``——跟请求头无关，是 TLS 层。

2. **真 Chrome + CDP**（兜底）：HTTP 通路被拦时退回浏览器，行为同旧版。
   中国国内航线会跳到 ``/chinaflights/ShowFareFirst`` 并改调普通
   ``FlightListSearch``（非 SSE），响应结构一样。

每个 itinerary::

    journeyList[].transSectionList[]  航段：flightInfo.flightNo / airlineCode、
                                      departPoint/arrivePoint 的 cityCode/airportCode、
                                      departDateTime / arriveDateTime、duration
    policies[]                        运价档：price.totalPrice / adult.salePrice / tax、
                                      policyMainGrade（Y/C/F）、seatCount、policyFlags
"""
import copy
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from curl_cffi import requests as curl_requests

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.browser import real_chrome
from core.currency import to_cny
from core.flight_time import TZ_CN, airport_timezone
from core.registry import resolve_city
from core.schema import Baggage, Money, Segment, TripOffer

CABIN_CN = {"Y": "经济舱", "W": "超级经济舱", "C": "商务舱", "J": "商务舱", "F": "头等舱"}
AIRLINE_CN = {
    "Japan Airlines": "日本航空", "All Nippon Airways": "全日空", "ANA": "全日空",
    "China Southern Airlines": "南方航空", "China Eastern Airlines": "东方航空",
    "Air China": "中国国航", "Asiana Airlines": "韩亚航空", "Korean Air": "大韩航空",
    "Cathay Pacific": "国泰航空", "Vietnam Airlines": "越南航空",
    "Xiamen Airlines": "厦门航空", "Shenzhen Airlines": "深圳航空",
    "Hainan Airlines": "海南航空", "Spring Airlines": "春秋航空",
    "EVA Air": "长荣航空", "China Airlines": "中华航空",
    "Thai Airways International": "泰国航空", "Singapore Airlines": "新加坡航空",
    "Peach": "乐桃航空", "Jetstar": "捷星航空", "Vanilla Air": "香草航空",
    "Sichuan Airlines": "四川航空", "Shandong Airlines": "山东航空",
    "Juneyao Airlines": "吉祥航空", "Shanghai Airlines": "上海航空",
}
SEARCH_URL = ("https://www.trip.com/flights/showfarefirst?"
              "dcity={o}&acity={d}&ddate={date}&triptype=ow&class={cls}"
              "&quantity=1&locale=zh-CN&curr=CNY")
SSE_KEY = "FlightListSearchSSE"
LIST_KEY = "FlightListSearch"
SSE_ENDPOINT = "https://www.trip.com/restapi/soa2/27015/FlightListSearchSSE"

# 搜索请求体模板。除了 journeyInfoTypes（航线/日期）和 clientTime，其余按浏览器里的原样保留
# —— 实测 head 里的 cid / abtList 不能省，省了只回一个空错误。
_BODY_TEMPLATE = {
    "mode": 0,
    "searchCriteria": {
        "grade": 1, "realGrade": 1, "tripType": 1, "journeyNo": 1,
        "passengerInfoType": {"adultCount": 1, "childCount": 0, "infantCount": 0},
        "journeyInfoTypes": [{"journeyNo": 1, "departDate": "", "departCode": "",
                              "arriveCode": "", "departAirport": "", "arriveAirport": ""}],
        "policyId": None,
    },
    "sortInfoType": {"direction": True, "orderBy": "Direct", "topList": []},
    "tagList": [],
    "flagList": ["NEED_RESET_SORT", "FullDataCache"],
    "filterType": {"filterFlagTypes": [], "queryItemSettings": [],
                   "studentsSelectedStatus": True},
    "abtList": [
        {"abCode": "251023_IBU_pricetool", "abVersion": "A"},
        {"abCode": "260302_IBU_farecardjc", "abVersion": "B"},
        {"abCode": "260518_IBU_tolwftjpx", "abVersion": "B"},
        {"abCode": "260622_IBU_XDirectLog", "abVersion": "B"},
        {"abCode": "250811_IBU_wjrankol", "abVersion": "B"},
        {"abCode": "260819_IBU_OYDAN", "abVersion": "B"},
        {"abCode": "260910_IBU_toldctjpx", "abVersion": "B"},
        {"abCode": "261102_IBU_CSUPGDOL", "abVersion": "A"},
    ],
    "head": {
        "cid": "", "ctok": "", "cver": "3", "lang": "01", "sid": "8888",
        "syscode": "40", "auth": "", "xsid": "",
        "extension": [
            {"name": "source", "value": "ONLINE"},
            {"name": "sotpGroup", "value": "Trip"},
            {"name": "sotpLocale", "value": "en-XX"},
            {"name": "sotpCurrency", "value": "CNY"},
            {"name": "allianceID", "value": "0"},
            {"name": "sid", "value": "0"},
            {"name": "ouid", "value": ""},
            {"name": "uuid"},
            {"name": "useDistributionType", "value": "1"},
            {"name": "vid", "value": ""},
            {"name": "pvid", "value": "1"},
            {"name": "Flt_SessionId", "value": "1"},
            {"name": "channel"},
            {"name": "x-ua", "value": "v=3_os=ONLINE_osv=10"},
            {"name": "PageId", "value": "10320667452"},
            {"name": "clientTime", "value": ""},
            {"name": "Flt_BatchId", "value": ""},
            {"name": "BlockTokenTimeout", "value": "0"},
            {"name": "full_link_time_scene", "value": "pure_list_page"},
            {"name": "xproduct", "value": "baggage"},
            {"name": "hotelEntrance", "value": "Flight"},
            {"name": "units", "value": "METRIC"},
            {"name": "sotpUnit", "value": "METRIC"},
        ],
        "Locale": "en-XX", "Language": "en", "Currency": "CNY",
        "ClientID": "", "appid": "700020",
    },
}
CABIN_GRADE = {"经济舱": 1, "超级经济舱": 1, "商务舱": 2, "头等舱": 3}


def _build_body(from_code: str, to_code: str, date: str, grade: int = 1) -> dict:
    body = copy.deepcopy(_BODY_TEMPLATE)
    crit = body["searchCriteria"]
    crit["grade"] = crit["realGrade"] = grade
    crit["journeyInfoTypes"][0].update(departDate=date, departCode=from_code,
                                       arriveCode=to_code)
    for ext in body["head"]["extension"]:
        name = ext.get("name")
        if name == "clientTime":
            ext["value"] = datetime.now().astimezone().isoformat(timespec="seconds")
        elif name == "Flt_BatchId":
            ext["value"] = f"travel-{int(time.time() * 1000)}"
    return body


def _fetch_http(url: str, body: dict, timeout: float = 45.0) -> dict:
    """curl_cffi 冒充 Chrome 的 TLS 指纹直接发请求。

    先用 GET 打开搜索页拿 cookie（Trip.com 要求先有会话），再 POST 拿 SSE。
    普通 ``requests`` 在这里会拿到 ``HTTP 430 whaleguard block``。
    """
    import uuid

    body = copy.deepcopy(body)
    for ext in body["head"]["extension"]:
        if ext.get("name") == "Flt_BatchId" and not ext.get("value"):
            ext["value"] = str(uuid.uuid4())
        elif ext.get("name") == "clientTime" and not ext.get("value"):
            ext["value"] = datetime.now().astimezone().isoformat(timespec="seconds")

    with curl_requests.Session(impersonate="chrome") as session:
        session.get(url, timeout=timeout)
        resp = session.post(
            SSE_ENDPOINT, data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json; charset=utf-8",
                "accept": "text/event-stream",
                "origin": "https://www.trip.com",
                "referer": url,
            },
            timeout=timeout,
        )
    if resp.status_code != 200:
        raise AdapterError(f"Trip.com HTTP {resp.status_code}：{resp.text[:80]}")
    payload = _parse_sse(resp.text)
    if not payload or not payload.get("itineraryList"):
        raise AdapterError(f"Trip.com 没返回行程（{len(resp.content)}B）：{resp.text[:120]}")
    return payload


def _parse_sse(text: str) -> Optional[dict]:
    """SSE 通常是单条超长 ``data:{...}``；万一被拆成多行，逐个试。"""
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text[5:] if text.startswith("data:") else text]
    candidates += [line.strip()[5:] for line in text.splitlines()
                   if line.strip().startswith("data:")]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _price_total(policy: dict) -> float:
    return float((policy.get("price") or {}).get("totalPrice") or 0)


def _pick_policy(policies: list[dict]) -> Optional[dict]:
    usable = [p for p in policies or [] if _price_total(p) > 0]
    return min(usable, key=_price_total) if usable else None


def _bag_cn(text: Optional[str]) -> Optional[str]:
    """'Checked baggage: 2 x 23 kg' -> '托运 2 x 23 kg'，给中文界面直接用。"""
    if not text:
        return None
    for prefix, cn in (("Carry-on baggage:", "手提"), ("Checked baggage:", "托运")):
        if text.startswith(prefix):
            return f"{cn} {text[len(prefix):].strip()}"
    return text


def _bag_text(policy: dict, key: str) -> Optional[str]:
    for tag in policy.get("tagList") or []:
        if tag.get("key") != key:
            continue
        for ext in tag.get("extendInfo") or []:
            if ext.get("extendKey") == "bagFormat":
                return _bag_cn(ext.get("extendValue"))
        return _bag_cn(tag.get("content"))
    return None


def _baggage(policy: dict) -> Optional[Baggage]:
    flags = policy.get("policyFlags") or []
    checked = _bag_text(policy, "FREE_CHECKED_BAGGAGE") if "FREE_CHECKED_BAGGAGE" in flags else None
    carry = (_bag_text(policy, "FREE_CARRY_ON_BAGGAGE")
             if "FREE_CARRY_ON_BAGGAGE" in flags else None)
    if not checked and not carry:
        return None
    return Baggage(carry_on=carry, checked=checked, note=None)


def _tz_map(payload: dict) -> dict[str, timezone]:
    """cityList 的 timeZone 是小时数，伦敦是 0——不能用 `or 8`，0 会被当成没值。"""
    tz: dict[str, timezone] = {}
    for city in payload.get("cityList") or []:
        if not city.get("code"):
            continue
        hours = city.get("timeZone")
        tz[city["code"]] = timezone(timedelta(hours=int(hours))) if hours is not None \
            else airport_timezone(city["code"])
    return tz


def _airlines(payload: dict) -> dict[str, str]:
    return {a["code"]: AIRLINE_CN.get(a.get("name") or "", a.get("name") or "")
            for a in payload.get("airlineList") or [] if a.get("code")}


def _segments(itinerary: dict, tz: dict, airlines: dict) -> list[Segment]:
    segments: list[Segment] = []
    for journey in itinerary.get("journeyList") or []:
        for sec in journey.get("transSectionList") or []:
            if (sec.get("transportType") or "FLIGHT") != "FLIGHT":
                continue
            dep = sec.get("departPoint") or {}
            arr = sec.get("arrivePoint") or {}
            dep_city = dep.get("cityCode") or ""
            arr_city = arr.get("cityCode") or ""
            info = sec.get("flightInfo") or {}
            code = info.get("airlineCode") or ""
            segments.append(Segment(
                mode="flight",
                trip_no=info.get("flightNo") or "",
                operator=airlines.get(code) or code,
                origin_code=dep.get("airportCode") or dep_city,
                origin_name=f"{dep.get('cityName', '')} {dep.get('terminal', '')}".strip(),
                destination_code=arr.get("airportCode") or arr_city,
                destination_name=f"{arr.get('cityName', '')} {arr.get('terminal', '')}".strip(),
                depart_at=datetime.fromisoformat(sec["departDateTime"]).replace(
                    tzinfo=tz.get(dep_city, timezone(timedelta(hours=8)))).isoformat(),
                arrive_at=datetime.fromisoformat(sec["arriveDateTime"]).replace(
                    tzinfo=tz.get(arr_city, timezone(timedelta(hours=8)))).isoformat(),
                duration_min=int(sec.get("duration") or 0),
            ))
    if len(segments) > 1:
        segments[0].stops = len(segments) - 1
        segments[0].via = [s.origin_code for s in segments[1:]]
    return segments


def parse_itinerary(itinerary: dict, tz: dict, airlines: dict, query: SearchQuery,
                    booking_url: str, fetched_at: str) -> Optional[TripOffer]:
    segments = _segments(itinerary, tz, airlines)
    policy = _pick_policy(itinerary.get("policies") or [])
    if not segments or not policy:
        return None

    total = _price_total(policy)
    adult = (policy.get("price") or {}).get("adult") or {}
    grade = policy.get("policyMainGrade") or "Y"
    cabin = CABIN_CN.get(grade, query.cabin or "经济舱")
    numbers = "+".join(s.trip_no for s in segments)
    key = (f"flight:{segments[0].origin_code}-{segments[-1].destination_code}:"
           f"{numbers}:{segments[0].depart_at}:{segments[-1].arrive_at}")
    policy_id = policy.get("policyId") or ""

    return TripOffer(
        offer_id=f"trip:{policy_id[:80]}",
        mode="flight",
        source="trip",
        source_name="Trip.com",
        segments=segments,
        price=Money(
            amount=total, currency="CNY", amount_cny=to_cny(total, "CNY"),
            base=float(adult.get("salePrice") or 0) or None,
            tax=float(adult.get("tax") or 0) or None,
        ),
        fare_class=cabin,
        price_note=f"{cabin}含税总价（Trip.com，舱位 {grade}）",
        baggage=_baggage(policy),
        seats_left=policy.get("seatCount"),
        booking_url=booking_url,
        fetched_at=fetched_at,
        seller="Trip.com",
        seller_type="ota",
        itinerary_key=key,
        raw={"policy_id": policy_id[:80],
             "price_options": len(itinerary.get("policies") or []),
             "flags": (policy.get("policyFlags") or [])[:6]},
    )


class FlightTripAdapter(BaseAdapter):
    """Trip.com 国际机票：curl_cffi 直连为主，真 Chrome 兜底。"""

    source = "trip"
    source_name = "Trip.com"
    supported_modes = {"flight"}

    def __init__(self, timeout: float = 30.0, use_browser: bool = True, *, http_timeout: float = 45.0):
        self.timeout = timeout
        self.use_browser = use_browser   # False = 只走 HTTP，永不弹窗（手机上就是这个模式）
        self.http_timeout = http_timeout

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        airports_o = origin.get("airports") or []
        airports_d = destination.get("airports") or []
        if not airports_o or not airports_d:
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")
        from_code = origin.get("code") or airports_o[0]
        to_code = destination.get("code") or airports_d[0]
        cabin = {"商务舱": "c", "头等舱": "f"}.get(query.cabin or "", "y")
        url = SEARCH_URL.format(o=from_code.lower(), d=to_code.lower(),
                                date=query.date, cls=cabin)

        payload: Optional[dict] = None
        http_error: Optional[Exception] = None
        try:
            payload = _fetch_http(url, _build_body(from_code, to_code, query.date,
                                                   CABIN_GRADE.get(query.cabin or "", 1)),
                                  timeout=self.http_timeout)
        except Exception as e:
            http_error = e

        if payload is None:
            if not self.use_browser:
                raise http_error or AdapterError("Trip.com HTTP 通路失败")
            try:
                payload = self._search_browser(url)
            except Exception as e:
                raise AdapterError(f"HTTP 失败（{http_error}）；浏览器也失败：{e}")

        tz = _tz_map(payload)
        airlines = _airlines(payload)
        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        offers: list[TripOffer] = []
        for it in payload.get("itineraryList") or []:
            try:
                offer = parse_itinerary(it, tz, airlines, query, url, fetched_at)
            except Exception:
                continue
            if offer:
                offers.append(offer)
        if not offers:
            raise AdapterError("Trip.com 返回了数据，但没解析出任何带价格的行程")
        return offers

    def _search_browser(self, url: str) -> dict:
        """兜底：起真 Chrome，监听它自己发的 FlightListSearch(SSE)。"""
        payloads: list[dict] = []

        def on_response(resp) -> None:
            url_ = resp.url
            if SSE_KEY in url_:
                try:
                    text = resp.text()
                except Exception:
                    return
                data = _parse_sse(text)
            elif LIST_KEY in url_:
                # 国内航线走普通 JSON 接口，结构一样
                try:
                    data = resp.json()
                except Exception:
                    return
            else:
                return
            if data and data.get("itineraryList"):
                payloads.append(data)

        with real_chrome() as browser:
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                raise AdapterError(f"Trip.com 页面打不开：{type(e).__name__}: {e}")

            # SSE 一次给全量；等它到齐，或者超时。偶尔会有第二次「重查」流，取最大的。
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                page.wait_for_timeout(800)
                if payloads:
                    page.wait_for_timeout(1500)
                    break

        if not payloads:
            raise AdapterError("Trip.com 没有返回 FlightListSearch 数据（可能被风控）")
        return max(payloads, key=lambda p: len(p.get("itineraryList") or []))

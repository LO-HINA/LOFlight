"""Google Flights「预订选项」adapter：抓同一行程在多家 OTA / 航司官网的报价。

这是多平台比价的真正数据源——Google 把各家报价聚合在一处。

抓法（不拦截请求，避免破坏前端渲染）：
1. 打开结果页，读每张航班卡片的文本：``05:45 – 13:45 / 国泰航空 / 7 小时 /
   CAN–HND / 经停 1 次 / 1 小时 50 分钟 HKG``；
2. 点开卡片，读右侧「预订选项」面板：
   ``通过 Asiana Airlines 预订 航空公司 ¥1,732`` / ``通过 Gotogate 预订 ¥1,880 US$282``；
3. 依次点「继续」截下 OTA 真实下单地址（gotogate.com、航司官网等）。

已验证：CAN→HND 2026-12-01 第 5 条（韩亚经首尔）返回
Asiana Airlines ¥1,732 / Shenzhen ¥4,996 / Gotogate ¥1,880。

限制：
- 只抓 Google 已列出的商家（通常 1-3 家）；「查看选项」展开被遮罩挡住；
- 卡片不显示航班号，trip_no 只能用航司名占位；
- 每个行程约 8-12 秒，默认前 5 个，用 ``top_n`` 控制。
"""
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fast_flights import FlightQuery, Passengers, create_query
from playwright.sync_api import sync_playwright

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.flight_time import TZ_CN, airport_timezone
from core.registry import resolve_city
from core.schema import Money, Segment, TripOffer

AIRLINE_CN = {
    "Cathay Pacific": "国泰航空", "China Southern": "南方航空",
    "China Eastern": "东方航空", "Air China": "中国国航",
    "Shenzhen": "深圳航空", "Xiamen Air": "厦门航空", "Hainan Airlines": "海南航空",
    "JAL": "日本航空", "ANA": "全日空", "Asiana Airlines": "韩亚航空",
    "Korean Air": "大韩航空", "Vietnam Airlines": "越南航空",
    "Vietjet Air": "越捷航空", "AirAsia": "亚洲航空", "Thai Airways": "泰国航空",
    "Singapore Airlines": "新加坡航空", "Scoot": "酷航",
    "EVA Air": "长荣航空", "China Airlines": "中华航空",
}

OPTION_RE = re.compile(
    r"通过(?P<name>.+?)预订(?P<airline>航空公司)?\s*¥(?P<cny>[\d,]+)"
    r"(?:\s*US\$(?P<usd>[\d,]+))?"
)
CLOCK_RE = re.compile(r"(\d{1,2}):(\d{2})(?:\+(\d))?")
ROUTE_RE = re.compile(r"\b([A-Z]{3})[–-]([A-Z]{3})\b")
DUR_RE = re.compile(r"(\d+)\s*小时(?:\s*(\d+)\s*分钟)?")
STOPS_RE = re.compile(r"直达|经停\s*(\d+)\s*次")
VIA_RE = re.compile(r"(?:小时|分钟)\s+([A-Z]{3})\b")

PANEL_JS = """() => {
  const t = document.body.innerText;
  const i = t.indexOf('预订选项');
  if (i < 0) return null;
  const j = t.indexOf('价格分析');
  return t.slice(i, j > i ? j : i + 1500);
}"""


def _num(text: Optional[str]) -> Optional[float]:
    return float(text.replace(",", "")) if text else None


def _seller_display(name: str, is_airline: bool) -> tuple[str, str]:
    if is_airline:
        return f"{AIRLINE_CN.get(name, name)}官网", "airline"
    return name, "ota"


def parse_card(text: str) -> Optional[dict]:
    """从卡片文本解析出发/到达时刻、航司、机场、经停。"""
    clocks = CLOCK_RE.findall(text)
    if len(clocks) < 2:
        return None
    route = ROUTE_RE.search(text)
    dur = DUR_RE.search(text)
    if not route or not dur:
        return None
    stops_m = STOPS_RE.search(text)
    stops = 0 if (stops_m and stops_m.group(0) == "直达") else (
        int(stops_m.group(1)) if stops_m and stops_m.group(1) else 0)

    # 航司名在时长那一行的上一行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    airline = ""
    for k, ln in enumerate(lines):
        if DUR_RE.search(ln) and k and lines[k - 1] not in ("–", "-"):
            airline = lines[k - 1]
            break

    def _clock(m):
        return int(m[0]), int(m[1]), int(m[2] or 0)

    via = [c for c in VIA_RE.findall(text) if c not in (route.group(1), route.group(2))]
    return {
        "dep": _clock(clocks[0]),
        "arr": _clock(clocks[1]),
        "from": route.group(1),
        "to": route.group(2),
        "duration_min": int(dur.group(1)) * 60 + int(dur.group(2) or 0),
        "stops": stops,
        "via": via,
        "airline": airline or "未知航司",
    }


class FlightGoogleBookingAdapter(BaseAdapter):
    source = "google_flights"
    source_name = "Google Flights"

    def __init__(self, top_n: int = 5, with_links: bool = True,
                 headless: bool = True, retries: int = 3):
        self.top_n = max(1, top_n)
        self.with_links = with_links
        self.headless = headless
        self.retries = max(1, retries)

    def search(self, query: SearchQuery) -> list[TripOffer]:
        origin = resolve_city(query.origin)
        destination = resolve_city(query.destination)
        # 用城市码（TYO / BJS / SHA）而不是单个机场，才能和携程覆盖同一批航班，
        # 比价时分到同一组；没有城市码的老数据退回第一个机场。
        from_ap = origin.get("code") or (origin.get("airports") or [None])[0]
        to_ap = destination.get("code") or (destination.get("airports") or [None])[0]
        if not from_ap or not to_ap:
            raise AdapterError(f"{query.origin} 或 {query.destination} 没有配置机场")

        url = self._search_url(from_ap, to_ap, query)
        last_err = ""

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=self.headless)
            try:
                for attempt in range(self.retries):
                    ctx = browser.new_context(locale="zh-CN",
                                              viewport={"width": 1440, "height": 1000})
                    page = ctx.new_page()
                    page.set_default_timeout(30000)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(7000 + attempt * 3000)
                        total = page.locator("li.pIav2d").count()
                        if not total:
                            last_err = "Google Flights 没有返回航班卡片（限流或当天无票）"
                            ctx.close()
                            continue

                        offers: list[TripOffer] = []
                        for i in range(min(self.top_n, total)):
                            if i:
                                self._back_to_results(page, url)
                            try:
                                offers.extend(self._collect_one(page, ctx, i, query, url))
                            except Exception as e:
                                last_err = f"第 {i+1} 个行程抓取失败：{type(e).__name__}: {e}"
                        if offers:
                            return offers
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {e}"
                    finally:
                        ctx.close()
            finally:
                browser.close()
        raise AdapterError(last_err or "没有抓到任何预订选项")

    # ---------- 单个行程 ----------

    def _collect_one(self, page, ctx, index: int, query: SearchQuery,
                     url: str) -> list[TripOffer]:
        card = page.locator("li.pIav2d").nth(index)
        info = parse_card(card.inner_text())
        if not info:
            return []

        card.click()
        panel = None
        for _ in range(10):
            page.wait_for_timeout(1000)
            panel = page.evaluate(PANEL_JS)
            if panel:
                break
        if not panel:
            return []

        options = list(OPTION_RE.finditer(panel))
        if not options:
            return []

        booking_page = page.url
        links = self._capture_links(page, ctx, len(options)) if self.with_links else []

        segments = self._segments(info, query.date)
        numbers = "+".join(s.trip_no for s in segments)
        key = (f"flight:{segments[0].origin_code}-{segments[-1].destination_code}:"
               f"{numbers}:{segments[0].depart_at}:{segments[-1].arrive_at}")

        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        offers: list[TripOffer] = []
        for idx, m in enumerate(options):
            name = m.group("name").strip()
            is_airline = bool(m.group("airline"))
            cny = _num(m.group("cny"))
            usd = _num(m.group("usd"))
            if not cny:
                continue
            display, stype = _seller_display(name, is_airline)
            link = links[idx] if idx < len(links) and links[idx] else booking_page
            offers.append(TripOffer(
                offer_id=f"google_flights:{key}:{name}",
                mode="flight",
                source=self.source,
                source_name=self.source_name,
                segments=[Segment(**{**s.__dict__, "via": list(s.via)}) for s in segments],
                price=Money(amount=cny, currency="CNY", amount_cny=to_cny(cny, "CNY")),
                fare_class=query.cabin or "经济舱",
                price_note=(f"{query.cabin or '经济舱'}含税总价；"
                            + ("航空公司直销" if is_airline else "OTA 出票")
                            + (f"；平台标价 US${usd:.0f}" if usd else "")),
                booking_url=link,
                fetched_at=fetched_at,
                seller=display,
                seller_type=stype,
                itinerary_key=key,
                raw={"google_seller": name, "is_airline": is_airline, "usd_price": usd},
            ))
        return offers

    @staticmethod
    def _segments(info: dict, date: str) -> list[Segment]:
        d_h, d_m, d_plus = info["dep"]
        a_h, a_m, a_plus = info["arr"]
        tz_from = airport_timezone(info["from"])
        start = datetime.fromisoformat(f"{date}T00:00:00").replace(tzinfo=tz_from)
        depart = start.replace(hour=d_h, minute=d_m)
        arrive = start.replace(hour=a_h, minute=a_m)
        if a_plus or arrive < depart:
            arrive += timedelta(days=a_plus or 1)
        return [Segment(
            mode="flight",
            trip_no=info["airline"],
            operator=info["airline"],
            origin_code=info["from"],
            origin_name=info["from"],
            destination_code=info["to"],
            destination_name=info["to"],
            depart_at=depart.isoformat(),
            arrive_at=arrive.isoformat(),
            duration_min=info["duration_min"],
            stops=info["stops"],
            via=info["via"],
        )]

    @staticmethod
    def _capture_links(page, ctx, count: int) -> list[str]:
        urls: list[str] = []
        buttons = page.locator("button", has_text="继续")
        for i in range(min(buttons.count(), count)):
            link = ""
            try:
                with ctx.expect_page(timeout=8000) as info:
                    buttons.nth(i).click(timeout=6000)
                popup = info.value
                popup.wait_for_timeout(2000)
                link = popup.url or ""
                if link.startswith("chrome-error"):
                    link = ""
                popup.close()
            except Exception:
                link = ""
            urls.append(link)
        return urls

    @staticmethod
    def _back_to_results(page, url: str) -> None:
        try:
            page.go_back(wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            if page.locator("li.pIav2d").count() >= 1:
                return
        except Exception:
            pass
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

    @staticmethod
    def _search_url(from_ap: str, to_ap: str, query: SearchQuery) -> str:
        q = create_query(
            flights=[FlightQuery(date=query.date, from_airport=from_ap, to_airport=to_ap)],
            seat="economy", trip="one-way",
            passengers=Passengers(adults=max(1, query.passengers)),
            currency="CNY", language="zh-CN",
        )
        params = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in q.params().items())
        return f"https://www.google.com/travel/flights?{params}"

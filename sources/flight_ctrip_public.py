"""从携程公开航线页读取单程航班起价。"""

import re
from datetime import date, datetime, timezone

from curl_cffi import requests
from selectolax.lexbor import LexborHTMLParser

from core.adapter import AdapterError
from core.flight_time import TZ_CN

BASE_URL = "https://flights.ctrip.com"
PRICE = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
FLIGHT_NUMBER = re.compile(r"[A-Z0-9]{2}\s?\d{2,4}[A-Z]?(?![A-Z0-9])")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
CABIN = re.compile(r"超级经济舱|经济舱|公务舱|商务舱|头等舱")


def node_text(node, selector):
    match = node.css_first(selector)
    return match.text(separator=" ", strip=True) if match else ""


def parse_quotes(html, departure):
    doc = LexborHTMLParser(html)
    listing = doc.css_first(".seo-flight-list")
    if listing is None:
        raise ValueError("页面没有公开航班列表")

    date_label = node_text(listing, ".list-header .date-info .day")
    actual_date = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", date_label)
    if actual_date is None or tuple(map(int, actual_date.groups())) != (departure.month, departure.day):
        raise ValueError(f"页面日期与查询不符（页面日期为 {date_label or '未显示'}）")

    offers = []
    for card in listing.css(".list-content-item-oneway, .list-content-item-transit"):
        price = PRICE.fullmatch(node_text(card, ".price-detail span.price"))
        depart_at = TIME.search(node_text(card, ".depart-box .time"))
        arrive_at = TIME.search(node_text(card, ".arrive-box .time"))
        numbers = []
        flight_labels = card.css(".airline .plane-No")
        for node in flight_labels:
            flight = FLIGHT_NUMBER.match(node.text(separator=" ", strip=True))
            if flight:
                numbers.append(flight.group().replace(" ", ""))
        if not (price and depart_at and arrive_at and numbers and len(numbers) == len(flight_labels)):
            continue
        amount = float(price.group(1).replace(",", ""))
        if amount <= 0:
            continue
        offers.append({
            "flight_numbers": numbers,
            "airlines": [n.text(separator=" ", strip=True) for n in card.css(".airline .airline-name")],
            "departure_time_local": depart_at.group().zfill(5),
            "arrival_time_local": arrive_at.group().zfill(5),
            "departure_airport": node_text(card, ".depart-box .airport"),
            "arrival_airport": node_text(card, ".arrive-box .airport"),
            "transfer": node_text(card, ".transfer-info") or None,
            "cabin_classes": list(dict.fromkeys(CABIN.findall(node_text(card, ".rate-detail")))),
            "price_from_cny": amount,
        })

    if not offers:
        raise ValueError("公开航班列表里没有可解析的报价")
    return offers


def fetch_public_quotes(origin: str, destination: str, departure: date, proxy: str | None = None) -> dict:
    if not all(re.fullmatch(r"[A-Z]{3}", code) for code in (origin, destination)):
        raise ValueError("出发和到达城市代码必须是三个英文字母")
    today = datetime.now(TZ_CN).date()
    offset = (departure - today).days
    if offset < 0:
        raise ValueError("出发日期已经过去（按北京时间计算）")
    url = f"{BASE_URL}/booking/{origin}-{destination}-day-{offset}.html"
    network_options = {"proxy": proxy} if proxy else {}
    try:
        response = requests.get(url, impersonate="chrome", timeout=15,
                                allow_redirects=False, **network_options)
    except requests.RequestsError as error:
        route = "本次指定的代理" if proxy else "当前环境的网络配置"
        raise AdapterError(f"使用{route}请求携程失败（{type(error).__name__}）") from error
    if 300 <= response.status_code < 400:
        raise AdapterError(f"携程将这个航线日期重定向到搜索页（HTTP {response.status_code}）")
    if response.status_code != 200:
        raise AdapterError(f"携程公开页返回 HTTP {response.status_code}")
    offers = parse_quotes(response.content, departure)

    return {
        "source": "ctrip_public_booking",
        "url": url,
        "departure_date": departure.isoformat(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "price_type": "starting_price",
        "offer_count": len(offers),
        "offers": offers,
    }

"""读取同程国内航班公开列表的经济舱展示价。"""
import re
from datetime import date

import requests
from selectolax.lexbor import LexborHTMLParser

from core.adapter import AdapterError

SEARCH_URL = "https://flight.ly.com/travel/flight/{origin}-{destination}?date={date}"
_FLIGHT = re.compile(r"\b[A-Z0-9]{2}\d{1,4}[A-Z]?\b")
_CLOCK = re.compile(r"\b\d{2}:\d{2}\b")
_AMOUNT = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")
_BOOKING = "https://m.ly.com/ft/touch/book15?"


def parse_domestic_quotes(html: bytes, departure: date, search_url: str) -> list[dict]:
    doc = LexborHTMLParser(html)
    if doc.css_first(".error-box"):
        raise AdapterError("同程国内机票页返回查询错误")
    date_input = doc.css_first('input[type="date"]')
    if not date_input or date_input.attributes.get("value") != departure.isoformat():
        raise AdapterError("同程国内机票页的日期与查询不符")

    quotes = []
    for card in doc.css("a.flight-card"):
        airline = card.css_first(".airline-tag")
        numbers = _FLIGHT.findall(airline.text(strip=True)) if airline else []
        times = card.css(".flight-time-col .time")
        if len(numbers) != 1 or len(times) != 2:
            continue
        start, end = [_CLOCK.search(node.text(strip=True)) for node in times]
        if not start or not end:
            continue
        for cabin in card.css(".cabin-row"):
            label = cabin.css_first(".cabin-name")
            price = cabin.css_first(".cabin-price .price")
            if not label or label.text(strip=True) != "经济舱" or not price:
                continue
            match = _AMOUNT.fullmatch(price.text(strip=True))
            if not match:
                continue
            amount = float(match[1].replace(",", ""))
            if amount <= 0:
                continue
            href = card.attributes.get("href", "")
            quotes.append({
                "seller": "同程旅行", "numbers": numbers, "price": amount,
                "departure": start[0], "arrival": end[0],
                "note": "公开展示价 · 税费未核实",
                "url": href if href.startswith(_BOOKING) else search_url,
            })
    return quotes


def fetch_domestic_quotes(origin: str, destination: str, departure: date) -> list[dict]:
    url = SEARCH_URL.format(origin=origin, destination=destination, date=departure.isoformat())
    response = requests.get(url, timeout=12)
    response.raise_for_status()
    return parse_domestic_quotes(response.content, departure, url)

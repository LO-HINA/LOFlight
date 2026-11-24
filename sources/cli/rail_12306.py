"""12306 高铁查询 adapter。

数据来源：https://kyfw.12306.cn/otn/leftTicket/queryG
已验证字段：
  3  车次        8  发车时刻     9  到达时刻     10 历时
  6/7 出发/到达站电码（用 data.map 换成中文）
  39 票价编码：座位类型(1位) + 整数价(4位) + 小数位(1位) + 余票(4位)，每 10 位一组
  21/23/24/25/26/28/29/30/31/32 各席别余票
"""
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.schema import Money, Segment, TripOffer

TZ_CN = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

STATION_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
INIT_URL = "https://kyfw.12306.cn/otn/leftTicket/init"
QUERY_ENDPOINTS = ("queryG", "query", "queryZ", "queryU", "queryA")

SEAT_NAMES = {
    "9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
    "D": "优选一等座", "1": "硬座", "2": "软座", "3": "硬卧",
    "4": "软卧", "6": "高级软卧", "A": "高级动卧", "F": "动卧",
    "J": "一等卧", "I": "二等卧", "W": "无座",
}
# result 行里各席别余票的下标
SEAT_COUNT_INDEX = {
    "9": 32, "P": 25, "M": 31, "O": 30, "D": None,
    "1": 29, "2": 24, "3": 28, "4": 23, "6": 21,
    "W": 26, "A": None, "F": None,
}


def _parse_duration(text: str) -> int:
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def _parse_yp_ex(text: str) -> dict[str, tuple[float, int]]:
    """解析票价字段，返回 {座位类型: (价格, 余票)}，同一席别只取第一组。"""
    out: dict[str, tuple[float, int]] = {}
    for i in range(0, len(text) - 9, 10):
        block = text[i:i + 10]
        seat_type = block[0]
        digits, count = block[1:6], block[6:10]
        if not digits.isdigit() or not count.isdigit():
            break
        price = int(digits[:4]) + int(digits[4]) / 10
        if seat_type not in out:
            out[seat_type] = (price, int(count))
    return out


def _availability(text: str) -> tuple[Optional[int], Optional[str]]:
    text = (text or "").strip()
    if not text:
        return None, None
    if text == "有":
        return None, "有"
    if text == "无":
        return 0, "无"
    if text.isdigit():
        return int(text), text
    return None, text


class Rail12306Adapter(BaseAdapter):
    source = "12306"
    source_name = "中国铁路12306"
    supported_modes = {"rail"}

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = Path(data_dir) if data_dir else Path(__file__).resolve().parents[2] / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._stations: Optional[dict[str, list[dict]]] = None

    # ---------- 站点表（本地缓存 7 天） ----------
    def _load_station_js(self) -> str:
        cache = self.data_dir / "12306_stations.js"
        if cache.exists() and time.time() - cache.stat().st_mtime < 7 * 86400:
            return cache.read_text(encoding="utf-8")
        resp = requests.get(STATION_URL, headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        cache.write_text(resp.text, encoding="utf-8")
        return resp.text

    def _station_index(self) -> dict[str, list[dict]]:
        if self._stations is not None:
            return self._stations
        text = self._load_station_js()
        m = re.search(r"station_names\s*=\s*'(.*)'", text, re.S)
        if not m:
            raise AdapterError("12306 站点表格式变化，未找到 station_names")
        index: dict[str, list[dict]] = {}
        for item in m.group(1).split("@"):
            if not item:
                continue
            parts = item.split("|")
            if len(parts) < 8:
                continue
            name, code, city = parts[1], parts[2], parts[7]
            index.setdefault(city, []).append({"name": name, "code": code})
        self._stations = index
        return index

    def _resolve_station(self, city: str) -> dict:
        candidates = self._station_index().get(city)
        if not candidates:
            raise AdapterError(f"12306 站点表里没有城市：{city}")
        # 城市级查询码 = 站名与城市名相同的那个站（上海 SHH、北京 BJP）
        for c in candidates:
            if c["name"] == city:
                return c
        return candidates[0]

    # ---------- 查询 ----------
    def search(self, query: SearchQuery) -> list[TripOffer]:
        try:
            datetime.strptime(query.date, "%Y-%m-%d")
        except ValueError:
            raise AdapterError(f"日期格式应为 YYYY-MM-DD：{query.date}")

        from_station = self._resolve_station(query.origin)
        to_station = self._resolve_station(query.destination)

        session = requests.Session()
        session.headers.update({
            "User-Agent": UA,
            "Referer": INIT_URL,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        })
        # 先访问 init 拿 JSESSIONID / BIGipServer cookie
        session.get(INIT_URL, timeout=20)

        params = {
            "leftTicketDTO.train_date": query.date,
            "leftTicketDTO.from_station": from_station["code"],
            "leftTicketDTO.to_station": to_station["code"],
            "purpose_codes": "ADULT",
        }
        payload, last_err = None, ""
        for endpoint in QUERY_ENDPOINTS:
            url = f"https://kyfw.12306.cn/otn/leftTicket/{endpoint}"
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code != 200:
                last_err = f"{endpoint} HTTP {resp.status_code}"
                continue
            try:
                data = resp.json()
            except ValueError:
                last_err = f"{endpoint} 返回非 JSON"
                continue
            if data.get("status") and data.get("data"):
                payload = data
                break
            last_err = f"{endpoint} 业务失败：{data.get('messages')}"
        if payload is None:
            raise AdapterError(f"12306 查询失败：{last_err}")

        return self._parse(payload, query, from_station, to_station)

    def _parse(self, payload: dict, query: SearchQuery, from_station: dict, to_station: dict) -> list[TripOffer]:
        data = payload["data"]
        station_map = data.get("map", {})
        offers: list[TripOffer] = []
        fetched_at = datetime.now(TZ_CN).isoformat(timespec="seconds")
        booking_url = self._booking_url(query, from_station, to_station)

        for row in data.get("result", []):
            f = row.split("|")
            if len(f) < 40:
                continue
            train_code = f[3]
            depart_clock, duration_text = f[8], f[10]
            if not train_code or not depart_clock:
                continue
            try:
                duration_min = _parse_duration(duration_text)
            except (ValueError, AttributeError):
                continue
            if duration_min <= 0:
                continue

            depart = datetime.strptime(
                f"{query.date} {depart_clock}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=TZ_CN)
            arrive = depart + timedelta(minutes=duration_min)

            from_code, to_code = f[6], f[7]
            from_name = station_map.get(from_code, from_station["name"])
            to_name = station_map.get(to_code, to_station["name"])

            seat_prices = _parse_yp_ex(f[39])
            if not seat_prices:
                continue

            for seat_type, (price, count) in seat_prices.items():
                if price <= 0:
                    continue
                seat_name = SEAT_NAMES.get(seat_type, f"席别{seat_type}")
                seats_left, avail_text = None, None
                idx = SEAT_COUNT_INDEX.get(seat_type)
                if idx is not None and idx < len(f):
                    seats_left, avail_text = _availability(f[idx])
                # yp_ex 里的余票数兜底（3000 是占位值，忽略）
                if seats_left is None and 0 <= count < 1000:
                    seats_left = count

                offers.append(TripOffer(
                    offer_id=f"12306:{train_code}:{query.date}:{from_code}-{to_code}:{seat_type}",
                    mode="rail",
                    source=self.source,
                    source_name=self.source_name,
                    segments=[Segment(
                        mode="rail",
                        trip_no=train_code,
                        operator="中国铁路",
                        origin_code=from_code,
                        origin_name=from_name,
                        destination_code=to_code,
                        destination_name=to_name,
                        depart_at=depart.isoformat(),
                        arrive_at=arrive.isoformat(),
                        duration_min=duration_min,
                        stops=0,
                    )],
                    price=Money(
                        amount=price,
                        currency="CNY",
                        amount_cny=to_cny(price, "CNY"),
                        base=price,
                        tax=0.0,
                        fee=0.0,
                    ),
                    fare_class=seat_name,
                    price_note=f"{seat_name}（12306 官方价）",
                    seats_left=seats_left,
                    availability=avail_text,
                    booking_url=booking_url,
                    fetched_at=fetched_at,
                ))
        return offers

    @staticmethod
    def _booking_url(query: SearchQuery, from_station: dict, to_station: dict) -> str:
        return (
            "https://kyfw.12306.cn/otn/leftTicket/init"
            f"?linktypeid=dc&fs={quote(from_station['name'])},{from_station['code']}"
            f"&ts={quote(to_station['name'])},{to_station['code']}"
            f"&date={query.date}&flag=N,N,Y"
        )

"""越南铁路 adapter：k.vnticketonline.vn 的公开时刻表 / 票价接口（无需验证码）。

数据源（GET，只需 Referer，无鉴权）：
- ``GTGV/LoadDmGa``                              全国 183 个车站
- ``GTGV/LoadDmTau?maGaDi&maGaDen&ngayDi``       某天某区间的车次
- ``GTGV/LoadOneTau?tauId&maGaDi``               该车次的经停站 + 各座位类型票价

已验证：Hà Nội → Sài Gòn 2026-12-01 返回 SE1/SE3/SE5/SE7/SE9 五趟，
SE7 有 12 种座位类型、1974 这种票价（单位：千 VND）。

限制：
- 接口不返回余票。余票只在 dsvn.vn 搜索页，那道入口要 reCAPTCHA，本 adapter 不碰。
- 票价是官方公布价，12go / baolau 等代理在这之上加服务费。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curl_cffi import requests

from core.adapter import AdapterError, BaseAdapter, SearchQuery
from core.currency import to_cny
from core.schema import Money, Segment, TripOffer

API = "https://k.vnticketonline.vn/api/GTGV/"
TZ_VN = timezone(timedelta(hours=7))
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
STATION_CACHE = _DATA_DIR / "vietnam_stations.json"
CACHE_SECONDS = 7 * 86400

# 城市 -> 越南铁路主站。越南铁路按站售票，一个城市取主站
CITY_STATIONS = {
    "河内": ("HNO", "Hà Nội"),
    "胡志明市": ("SGO", "Sài Gòn"),
    "岘港": ("DNA", "Đà Nẵng"),
    "顺化": ("HUE", "Huế"),
    "芽庄": ("NTR", "Nha Trang"),
}

# LoaiCho -> 中文席别。接口只给越南语全名，这里补中文，未知的回落原名
SEAT_CN = {
    "AnLT1M": "四人包厢·下铺",
    "AnLT1Mv": "四人包厢·下铺(VIP)",
    "AnLT2M": "四人包厢·上铺",
    "AnLT2Mv": "四人包厢·上铺(VIP)",
    "BnLT1M": "六人包厢·下铺",
    "BnLT2M": "六人包厢·中铺",
    "BnLT3M": "六人包厢·上铺",
    "GP": "硬座（加座）",
    "NML": "软座",
    "NMLV": "软座(VIP)",
    "NML56": "软座(56座)",
    "NML56V": "软座(56座/VIP)",
}


def _iso(day: str, clock: str) -> str:
    """'2026-12-01T00:00:00' + '06:00' -> '2026-12-01T06:00:00+07:00'"""
    d = datetime.fromisoformat(day[:10]).replace(tzinfo=TZ_VN)
    h, m = (int(x) for x in clock.split(":")[:2])
    return d.replace(hour=h, minute=m).isoformat()


class RailVietnamAdapter(BaseAdapter):
    source = "rail_vn"
    source_name = "越南铁路"
    supported_modes = {"rail"}

    def __init__(self) -> None:
        self.session = requests.Session(impersonate="chrome")
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://k.vnticketonline.vn/",
            "Accept-Language": "vi-VN,vi;q=0.9",
        })

    def _get(self, path: str, params: dict):
        r = self.session.get(API + path, params=params, timeout=30)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            raise AdapterError(f"越南铁路接口返回非 JSON：{path}")

    def _station_for(self, city: str) -> tuple[str, str]:
        hit = CITY_STATIONS.get(city)
        if not hit:
            raise AdapterError(
                f"越南铁路还没收录 {city}，可用城市：{'、'.join(CITY_STATIONS)}"
            )
        return hit

    def _stations(self) -> list[dict]:
        """车站表一周缓存一次，站点基本不变。"""
        if STATION_CACHE.exists() and time.time() - STATION_CACHE.stat().st_mtime < CACHE_SECONDS:
            try:
                return json.loads(STATION_CACHE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        data = self._get("LoadDmGa", {})
        if not isinstance(data, list) or not data:
            raise AdapterError("越南铁路车站表为空")
        try:
            STATION_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return data

    def search(self, query: SearchQuery) -> list[TripOffer]:
        self._stations()  # 预热/校验接口可用，返回结果本身用不到全部车站
        o_code, o_name = self._station_for(query.origin)
        d_code, d_name = self._station_for(query.destination)

        trains = self._get("LoadDmTau", {
            "maGaDi": o_code, "maGaDen": d_code, "ngayDi": query.date,
        })
        if not isinstance(trains, list) or not trains:
            raise AdapterError(f"{query.origin}→{query.destination} {query.date} 当天没有车次")

        fetched_at = datetime.now(TZ_VN).isoformat(timespec="seconds")
        offers: list[TripOffer] = []
        for train in trains:
            try:
                detail = self._get("LoadOneTau", {"tauId": train["Id"], "maGaDi": o_code})
            except Exception:
                detail = train  # 票价拿不到也保留车次，但下面会因无价格而跳过
            offers.extend(self._to_offers(
                query, train, detail, o_code, o_name, d_code, d_name, fetched_at))

        if not offers:
            raise AdapterError(
                f"{query.origin}→{query.destination} {query.date} 有车次但没有票价数据"
            )
        return offers

    def _to_offers(self, query, train, detail, o_code, o_name, d_code, d_name, fetched_at):
        prices = detail.get("BangGiaVes") or []
        if not prices:
            return []

        seg = Segment(
            mode="rail",
            trip_no=train.get("MacTau") or "?",
            operator="越南铁路",
            origin_code=o_code,
            origin_name=o_name,
            destination_code=d_code,
            destination_name=d_name,
            depart_at=_iso(train["NgayDi"], train["GioDi"]),
            arrive_at=_iso(train["NgayDen"], train["GioDen"]),
            duration_min=int(train.get("Duration") or 0),
            stops=0,
        )
        booking_url = (
            f"https://dsvn.vn/#/timve?gaDi={o_code}&gaDen={d_code}&ngayDi={query.date}"
        )

        offers: list[TripOffer] = []
        for row in prices:
            vnd = float(row.get("GiaVe") or 0) * 1000  # 接口单位是「千 VND」
            if vnd <= 0:
                continue
            code = row.get("LoaiCho") or ""
            offers.append(TripOffer(
                offer_id=(
                    f"rail_vn:{seg.trip_no}:{query.date}:{o_code}-{d_code}:{code}"
                ),
                mode="rail",
                source=self.source,
                source_name=self.source_name,
                segments=[seg],
                price=Money(
                    amount=vnd,
                    currency="VND",
                    amount_cny=to_cny(vnd, "VND"),
                ),
                fare_class=SEAT_CN.get(code) or row.get("TenLoaiCho") or code,
                price_note="越南铁路官方票价（未含代理服务费）；余票需到 dsvn.vn 确认",
                seats_left=None,
                availability=None,
                booking_url=booking_url,
                fetched_at=fetched_at,
                raw={
                    "LoaiCho": code,
                    "TenLoaiCho": row.get("TenLoaiCho"),
                    "GiaVeThousandVnd": row.get("GiaVe"),
                },
            ))
        return offers

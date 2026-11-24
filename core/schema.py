"""统一数据契约：所有来源（12306 / 携程 / Google Flights ...）的报价都归一化成 TripOffer。

字段设计原则：
- 排序只需要 price.amount_cny 和 total_duration_min，其它字段用于展示和判断"是不是同一个商品"
- 原始币种和人民币折算都保留，方便切换 VND / USD 展示
- 航班和火车共用同一结构，区别只在 mode / fare_class / baggage
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Money:
    amount: float                 # 原始币种金额
    currency: str                 # CNY / VND / USD
    amount_cny: float             # 折算成人民币，统一排序用
    base: Optional[float] = None  # 票面价
    tax: Optional[float] = None   # 税费
    fee: Optional[float] = None   # 服务费 / 手续费

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Segment:
    mode: str                     # flight / rail / bus
    trip_no: str                  # MU5101 / G2 / SE3
    operator: str                 # 中国国航 / 中国铁路 / Vietnam Railways
    origin_code: str              # PVG / AOH / SGN
    origin_name: str
    destination_code: str
    destination_name: str
    depart_at: str                # ISO8601，带时区，如 2026-11-20T06:43:00+08:00
    arrive_at: str
    duration_min: int
    stops: int = 0
    via: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Baggage:
    carry_on: Optional[str] = None   # "7kg"
    checked: Optional[str] = None    # "20kg"
    pieces: Optional[int] = None     # 件数
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TripOffer:
    offer_id: str                 # 唯一键，用于去重：source:trip_no:date:fare_class
    mode: str                     # flight / rail / bus
    source: str                   # 抓取来源：12306 / ctrip / google_flights
    source_name: str              # 中国铁路12306
    segments: list[Segment]
    price: Money
    fare_class: str               # 二等座 / 经济舱 / 软卧
    price_note: str               # "二等座（12306 官方价）" / "含税，不含托运行李"
    seats_left: Optional[int] = None
    availability: Optional[str] = None   # "有" / "无" / "3"
    baggage: Optional[Baggage] = None
    refundable: Optional[bool] = None
    changeable: Optional[bool] = None
    booking_url: Optional[str] = None
    fetched_at: str = ""
    raw: Optional[dict] = None    # 原始数据，仅调试时保留
    # 购票平台：source 是「从哪抓的」，seller 是「去哪买」。App 的标签用 seller。
    seller: Optional[str] = None          # 国泰航空官网 / Gotogate / 中国铁路12306
    seller_type: Optional[str] = None     # airline / ota / official
    itinerary_key: Optional[str] = None   # 同一行程（同航班/同车次同时刻）的分组键

    @property
    def total_duration_min(self) -> int:
        """第一段出发到最后一段到达的实际耗时，含中转等待。"""
        if not self.segments:
            return 0
        try:
            start = datetime.fromisoformat(self.segments[0].depart_at)
            end = datetime.fromisoformat(self.segments[-1].arrive_at)
            return max(0, int((end - start).total_seconds() // 60))
        except ValueError:
            # 时区缺失时退回"分段耗时 + 30 分钟衔接"
            return sum(s.duration_min for s in self.segments) + 30 * (len(self.segments) - 1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_duration_min"] = self.total_duration_min
        # App 直接渲染这个字段做平台标签；没抓到时退回数据源名
        d["seller_display"] = self.seller or self.source_name
        d["itinerary_key"] = self.itinerary_key or self.default_itinerary_key()
        return d

    def default_itinerary_key(self) -> str:
        """同一行程的判定键：班次序列 + 首末时刻 + 起降站。

        跨平台同航班必须落在同一个 key 上，才能算价差。
        """
        if not self.segments:
            return self.offer_id
        first, last = self.segments[0], self.segments[-1]
        numbers = "+".join(s.trip_no for s in self.segments)
        return (f"{self.mode}:{first.origin_code}-{last.destination_code}:"
                f"{numbers}:{first.depart_at}:{last.arrive_at}")

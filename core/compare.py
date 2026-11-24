"""同一行程的跨平台比价。

为什么要分组：用户要的不是「最便宜的票」，而是「同一张票，哪个平台最便宜」。
同一趟航班（同航班号、同时刻、同起降机场）在不同平台报价不同，
分组后才能看出价差，也才能回答「多花 148 块值不值」。

用法：
    from core.compare import build_itineraries
    groups = build_itineraries(offers)      # 按最低价排序
    for g in groups:
        print(g.cheapest.seller, g.spread)
"""
from dataclasses import dataclass, field
from typing import Optional

from core.registry import city_code_for
from core.schema import TripOffer


@dataclass
class Itinerary:
    """一组同行程、不同平台的报价。"""
    key: str
    offers: list[TripOffer] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 价格升序；同价时官方渠道优先（退改更省心）
        self.offers.sort(key=lambda o: (o.price.amount_cny, o.seller_type != "airline"))
        # 同一平台对同一行程只留最便宜那条：携程会把共享代码的同一趟航班
        # 拆成 CZ385 / 3U7051 / JL5032 三条，价格一样，展示成三行没有意义。
        seen: set[str] = set()
        unique: list[TripOffer] = []
        for offer in self.offers:
            name = offer.seller or offer.source
            if name in seen:
                continue
            seen.add(name)
            unique.append(offer)
        self.offers = unique

    @property
    def cheapest(self) -> TripOffer:
        return self.offers[0]

    @property
    def most_expensive(self) -> TripOffer:
        return self.offers[-1]

    @property
    def min_price(self) -> float:
        return self.cheapest.price.amount_cny

    @property
    def max_price(self) -> float:
        return self.most_expensive.price.amount_cny

    @property
    def spread(self) -> float:
        """价差：最高价 - 最低价。0 表示只有一个平台有价。"""
        return round(self.max_price - self.min_price, 2)

    @property
    def spread_pct(self) -> float:
        return round(self.spread / self.min_price * 100, 1) if self.min_price else 0.0

    @property
    def seller_count(self) -> int:
        return len(self.offers)

    @property
    def is_comparable(self) -> bool:
        """至少两个不同商家才谈得上「比价」。"""
        return len({o.seller or o.source for o in self.offers}) >= 2

    def delta_to_cheapest(self, offer: TripOffer) -> float:
        return round(offer.price.amount_cny - self.min_price, 2)

    @property
    def segments(self):
        return self.cheapest.segments

    def to_dict(self) -> dict:
        return {
            "itinerary_key": self.key,
            "min_price_cny": self.min_price,
            "max_price_cny": self.max_price,
            "spread_cny": self.spread,
            "spread_pct": self.spread_pct,
            "seller_count": self.seller_count,
            "offers": [o.to_dict() for o in self.offers],
        }


def group_key(offer: TripOffer) -> str:
    return offer.itinerary_key or offer.default_itinerary_key()


def _wall(iso: str) -> str:
    """去掉时区，只留本地墙钟时间。

    Google 给的是 ``2026-12-01T15:10:00+08:00``，携程给的是 ``2026-12-01 15:10:00``，
    同一条腿的"表面时间"是一样的，去掉时区就能对齐。
    """
    return (iso or "")[:16].replace(" ", "T")


def loose_key(offer: TripOffer) -> str:
    """跨平台对齐键：只看起降城市 + 本地起降时刻，不看航班号/航司/机场。

    Google Flights 的卡片不给航班号，携程给的是 ``CA1330``；用航班号分组永远对不上，
    而"同一城市、同一分钟起飞、同一分钟落地"基本就是同一趟航班（含代码共享）。
    机场统一归到城市级（HND/NRT -> TYO），否则携程按机场给、同程按城市给会对不上。
    """
    if not offer.segments:
        return offer.offer_id
    first, last = offer.segments[0], offer.segments[-1]
    return (f"{offer.mode}:{city_code_for(first.origin_code)}-"
            f"{city_code_for(last.destination_code)}:"
            f"{_wall(first.depart_at)}~{_wall(last.arrive_at)}")


def build_itineraries(offers: list[TripOffer], key_fn=group_key) -> list[Itinerary]:
    """把报价按行程分组，返回按最低价升序排列的行程列表。

    ``key_fn`` 默认用 adapter 自己给的精确键；跨平台合并时传 :func:`loose_key`。
    """
    buckets: dict[str, Itinerary] = {}
    for o in offers:
        key = key_fn(o)
        buckets.setdefault(key, Itinerary(key=key)).offers.append(o)
    groups = list(buckets.values())
    for g in groups:
        g.__post_init__()
    groups.sort(key=lambda g: (g.min_price, g.cheapest.total_duration_min))
    return groups


def seller_leaderboard(offers: list[TripOffer], key_fn=group_key) -> list[dict]:
    """统计每家平台「赢过几次最低价」，用来判断哪个平台整体更便宜。

    只有真有多家报价的行程才计入，避免单平台行程刷榜。
    """
    stats: dict[str, dict] = {}
    for g in build_itineraries(offers, key_fn=key_fn):
        if not g.is_comparable:
            continue
        win = g.cheapest.seller or g.cheapest.source_name
        for o in g.offers:
            name = o.seller or o.source_name
            row = stats.setdefault(name, {"seller": name, "quotes": 0, "wins": 0,
                                          "total_cny": 0.0, "type": o.seller_type})
            row["quotes"] += 1
            row["total_cny"] += o.price.amount_cny
            if name == win:
                row["wins"] += 1
    rows = list(stats.values())
    for r in rows:
        r["avg_cny"] = round(r["total_cny"] / r["quotes"], 2) if r["quotes"] else 0.0
        del r["total_cny"]
    rows.sort(key=lambda r: (-r["wins"], r["avg_cny"]))
    return rows


def cheapest_per_itinerary(offers: list[TripOffer]) -> list[TripOffer]:
    """每个行程只留最低价那条，用于和其它交通方式一起排序。"""
    return [g.cheapest for g in build_itineraries(offers)]


def compare_public_quotes(public: dict, offers: list[TripOffer], cabin: str) -> list[dict]:
    """对齐携程公开页起价与同航班、同舱等的其他平台展示价。"""
    def key(numbers, departure, arrival):
        return tuple(n.upper().replace(" ", "") for n in numbers), departure, arrival

    by_flight: dict[tuple, dict[str, TripOffer]] = {}
    for offer in offers:
        seller = offer.seller or offer.source_name
        if (not offer.segments or not offer.price.amount_cny > 0
                or seller.casefold() in {"携程", "ctrip", "携程旅行"}
                or offer.fare_class.replace("公务舱", "商务舱") != cabin):
            continue
        first, last = offer.segments[0], offer.segments[-1]
        departure = _wall(first.depart_at)
        if departure[:10] != public["departure_date"]:
            continue
        flight = key([segment.trip_no for segment in offer.segments],
                     departure[11:16], _wall(last.arrive_at)[11:16])
        sellers = by_flight.setdefault(flight, {})
        if seller not in sellers or offer.price.amount_cny < sellers[seller].price.amount_cny:
            sellers[seller] = offer

    matches = []
    for quote in public["offers"]:
        displayed_cabins = {item.replace("公务舱", "商务舱") for item in quote["cabin_classes"]}
        if displayed_cabins != {cabin}:
            continue
        flight = key(quote["flight_numbers"], quote["departure_time_local"], quote["arrival_time_local"])
        sellers = by_flight.get(flight)
        if not sellers:
            continue
        matches.append({
            "flight_numbers": quote["flight_numbers"],
            "departure_time_local": quote["departure_time_local"],
            "arrival_time_local": quote["arrival_time_local"],
            "cabin": cabin,
            "ctrip_price_from_cny": quote["price_from_cny"],
            "other_quotes": [
                {"seller": seller, "price_cny": offer.price.amount_cny, "price_note": offer.price_note,
                 "difference_from_ctrip_cny": round(offer.price.amount_cny - quote["price_from_cny"], 2),
                 "booking_url": offer.booking_url}
                for seller, offer in sorted(sellers.items(), key=lambda item: item[1].price.amount_cny)
            ],
        })
    return sorted(matches, key=lambda item: item["ctrip_price_from_cny"])

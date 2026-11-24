"""命令行入口：python cli.py 上海 北京 2026-11-20 [--mode rail] [--sort price]"""
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Callable, Optional

from core.adapter import SearchQuery
from core.compare import build_itineraries, compare_public_quotes, loose_key, seller_leaderboard
from core.registry import resolve_city, route_type
from sources.cli.flight_ctrip import FlightCtripAdapter
from sources.cli.flight_google import FlightGoogleAdapter
from sources.cli.flight_google_booking import FlightGoogleBookingAdapter
from sources.cli.flight_kiwi import FlightKiwiAdapter
from sources.cli.rail_12306 import Rail12306Adapter
from sources.cli.rail_vietnam import RailVietnamAdapter
from sources.flight_ctrip_public import fetch_public_quotes
from sources.flight_ly import FlightLyAdapter
from sources.flight_trip import FlightTripAdapter

SORT_NAMES = {"price": "价格", "duration": "耗时", "composite": "综合"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def print_compare(groups, origin: str, destination: str, date: str, limit: int = 20) -> None:
    """跨平台比价视图：一个行程一组，组内按价格排，标出差价。

    有多个平台报价的行程排前面——那才是用户要看的。
    """
    total = sum(g.seller_count for g in groups)
    comparable = sum(1 for g in groups if g.is_comparable)
    print(f"\n{origin} → {destination}  {date}  |  共 {len(groups)} 个行程、{total} 条报价"
          f"  |  其中 {comparable} 个行程有多个平台\n")

    ordered = sorted(groups, key=lambda g: (not g.is_comparable, g.min_price))
    shown = ordered[:limit]

    for i, g in enumerate(shown):
        s = g.segments[0]
        last = g.segments[-1]
        via = f" 经停 {s.stops} 次 {'/'.join(s.via)}" if s.stops else " 直达"
        num = CIRCLED[i] if i < len(CIRCLED) else f"{i + 1}."
        print(f"{num} {s.depart_at[11:16]}→{last.arrive_at[11:16]}  {s.operator}{via}"
              f"  {fmt_duration(g.cheapest.total_duration_min)}")
        width = max(len(o.seller or "") for o in g.offers)
        for o in g.offers:
            delta = "" if o is g.cheapest else f"  +¥{g.delta_to_cheapest(o):.0f}"
            tag = " ← 最低价" if o is g.cheapest else ""
            print(f"    {(o.seller or o.source_name):<{width}}  ¥{o.price.amount_cny:>7.0f}{delta}{tag}")
        if g.is_comparable:
            print(f"    价差 ¥{g.spread:.0f}（{g.spread_pct:.0f}%），最低价来自 "
                  f"{g.cheapest.seller}")
        else:
            print("    仅一个平台有报价，无法比价")
        print()

    if len(ordered) > len(shown):
        print(f"…… 还有 {len(ordered) - len(shown)} 个行程未显示（--limit 调整，"
              f"--json 输出全量）")


def short_station(name: str, code: str) -> str:
    """机场英文名太长，表格里用三字码；中文站名保留。"""
    return name if len(name) <= 10 else code


def fmt_duration(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}m"


def sort_offers(offers, key: str):
    if key == "price":
        return sorted(offers, key=lambda o: o.price.amount_cny)
    if key == "duration":
        return sorted(offers, key=lambda o: (o.total_duration_min, o.price.amount_cny))
    if not offers:
        return offers
    prices = [o.price.amount_cny for o in offers]
    durs = [o.total_duration_min for o in offers]
    pmin, pmax = min(prices), max(prices)
    dmin, dmax = min(durs), max(durs)

    def score(o):
        p = (o.price.amount_cny - pmin) / (pmax - pmin) if pmax > pmin else 0
        d = (o.total_duration_min - dmin) / (dmax - dmin) if dmax > dmin else 0
        return 0.6 * p + 0.4 * d

    return sorted(offers, key=score)


def print_table(offers) -> None:
    headers = ["班次", "席别", "出发站", "到达站", "出发", "到达", "历时", "中转", "价格(CNY)", "余票", "来源"]
    rows = []
    for o in offers:
        first, last = o.segments[0], o.segments[-1]
        via = "-" if len(o.segments) == 1 else "/".join(s.destination_code for s in o.segments[:-1])
        seats = o.availability or (str(o.seats_left) if o.seats_left is not None else "-")
        rows.append([
            first.trip_no,
            o.fare_class,
            short_station(first.origin_name, first.origin_code),
            short_station(last.destination_name, last.destination_code),
            first.depart_at[11:16],
            last.arrive_at[11:16],
            fmt_duration(o.total_duration_min),
            via,
            f"{o.price.amount_cny:.0f}",
            seats,
            o.source_name,
        ])
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]

    def fmt(vals):
        return "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(vals))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def _run_platform(name: str, factory: Callable, query: SearchQuery) -> tuple[str, list, Optional[Exception]]:
    """跑一个平台，异常吞掉返回，让其它平台的结果照常出。"""
    try:
        got = factory().search(query)
        print(f"[+] {name}: {len(got)} 条", file=sys.stderr)
        return name, got, None
    except Exception as e:
        print(f"[!] {name} 失败：{e}", file=sys.stderr)
        return name, [], e


def run_compare(args, origin: str, destination: str) -> int:
    """跨平台比价：同一行程，比各家平台报价。

    所选数据源并行查询，单个来源失败时仍展示其他结果：
    - 同程旅行（``ly``）：服务端渲染 HTML，2 秒拿 81 条；
    - 携程（``ctrip``）：真 Chrome + CDP 抓 batchSearch，175 条，20-40 秒；
    - 携程公开页（``ctrip-public``）：读取近期单程起价，按航班号、时刻和舱等匹配；
    - Trip.com（``trip``）：curl_cffi 冒充 Chrome TLS，纯 HTTP，5 秒 100+ 条；
    - Kiwi.com（``kiwi``）：GraphQL 纯 HTTP，4 秒，最便宜的 25 条聚合票；
    - Google Flights（``google``）：每个行程展开 1-3 家 OTA/航司官网，最慢。
    并行后总耗时 ≈ 最慢的那个，而不是五者相加。
    其他实时来源仍按「起降城市 + 墙钟时刻」对齐（Google 卡片不给航班号）。
    """
    query = SearchQuery(origin=origin, destination=destination, date=args.date, mode="flight")
    platforms = args.platforms
    known = {"ly", "ctrip", "ctrip-public", "trip", "kiwi", "google"}
    wanted = known if platforms == "all" else {p.strip() for p in platforms.split(",") if p.strip()}
    unknown = wanted - known
    if unknown:
        print(f"[!] 未知平台：{', '.join(sorted(unknown))}（可选 {', '.join(sorted(known))} 或 all）",
              file=sys.stderr)
        return 2

    jobs: list[tuple[str, Callable]] = []
    if "ly" in wanted:
        print("[*] 同程旅行：正在查国际航班列表（服务端渲染，约 2 秒）...", file=sys.stderr)
        jobs.append(("同程旅行", FlightLyAdapter))
    if "ctrip" in wanted:
        print("[*] 携程：正在启动真 Chrome 抓列表（约 20-40 秒，会弹出窗口）...", file=sys.stderr)
        jobs.append(("携程", FlightCtripAdapter))
    if "trip" in wanted:
        print("[*] Trip.com：正在查国际版列表（纯 HTTP，约 5 秒）...", file=sys.stderr)
        jobs.append(("Trip.com", FlightTripAdapter))
    if "kiwi" in wanted:
        print("[*] Kiwi.com：正在查 GraphQL（纯 HTTP，约 4 秒）...", file=sys.stderr)
        jobs.append(("Kiwi.com", FlightKiwiAdapter))
    if "google" in wanted:
        print(f"[*] Google Flights：正在逐个打开预订选项（前 {args.top} 个行程，约 1 分钟）...",
              file=sys.stderr)
        jobs.append(("Google Flights",
                     lambda: FlightGoogleBookingAdapter(top_n=args.top,
                                                        with_links=not args.no_links)))

    use_public = "ctrip-public" in wanted
    if not jobs and not use_public:
        print("[!] 没有选任何平台", file=sys.stderr)
        return 2

    if use_public:
        try:
            departure = date.fromisoformat(query.date)
        except ValueError:
            print("[!] 出发日期需要使用 YYYY-MM-DD。", file=sys.stderr)
            return 2

    offers = []
    failed = []
    public = None
    with ThreadPoolExecutor(max_workers=len(jobs) + int(use_public)) as pool:
        futures = [pool.submit(_run_platform, name, factory, query) for name, factory in jobs]
        if use_public:
            public_future = pool.submit(fetch_public_quotes, resolve_city(origin)["code"],
                                        resolve_city(destination)["code"], departure)
        for future in as_completed(futures):
            name, got, err = future.result()
            offers.extend(got)
            if err:
                failed.append((name, err))
        if use_public:
            try:
                public = public_future.result()
                print(f"[+] 携程公开页：{public['offer_count']} 条", file=sys.stderr)
            except Exception as error:
                failed.append(("携程公开页", error))
                print(f"[!] 携程公开页失败：{error}", file=sys.stderr)

    if not offers and not public:
        detail = "；".join(f"{name}: {err}" for name, err in failed)
        print(f"[!] 比价失败，所有平台都没有结果。{detail}", file=sys.stderr)
        return 1

    groups = build_itineraries(offers, key_fn=loose_key)
    if public:
        public["comparisons"] = compare_public_quotes(public, offers, query.cabin or "经济舱")
    if args.json:
        payload = {
            "query": {"origin": origin, "destination": destination,
                      "date": args.date, "cabin": query.cabin or "经济舱"},
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "itinerary_count": len(groups),
            "offer_count": len(offers),
            "sellers": seller_leaderboard(offers, key_fn=loose_key),
            "itineraries": [g.to_dict() for g in groups],
        }
        if public:
            payload["ctrip_public"] = public
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if groups:
            print_compare(groups, origin, destination, args.date, limit=args.limit)
        if public:
            price = min(offer["price_from_cny"] for offer in public["offers"])
            print(f"携程公开页共 {public['offer_count']} 条，最低 ¥{price:,.0f} 起")
            if not public["comparisons"]:
                print("未匹配到其他平台的同航班、同舱等报价。")
            for match in public["comparisons"][:args.limit]:
                print(f"  {' + '.join(match['flight_numbers'])}  "
                      f"{match['departure_time_local']}→{match['arrival_time_local']}  "
                      f"携程 ¥{match['ctrip_price_from_cny']:,.0f} 起")
                for other in match["other_quotes"]:
                    print(f"    {other['seller']} ¥{other['price_cny']:,.0f}"
                          f"（展示价差 ¥{other['difference_from_ctrip_cny']:+,.2f}）")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(description="多平台机票/火车票比价 - MVP")
    p.add_argument("origin", help="出发城市，如 上海")
    p.add_argument("destination", help="到达城市，如 北京")
    p.add_argument("date", help="出发日期 YYYY-MM-DD")
    p.add_argument("--mode", choices=["auto", "rail", "flight", "bus"], default="auto")
    p.add_argument("--sort", choices=["price", "duration", "composite"], default="price")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true", help="输出统一 JSON，便于喂给 App")
    p.add_argument("--include-soldout", action="store_true", help="包含无票的结果（默认过滤）")
    p.add_argument("--compare", action="store_true",
                   help="按行程分组做跨平台比价（同一航班，哪个平台最便宜）")
    p.add_argument("--top", type=int, default=5, help="比价时抓前几个行程，默认 5")
    p.add_argument("--no-links", action="store_true", help="比价时不抓 OTA 真实下单链接（更快）")
    p.add_argument("--platforms", default="all",
                   metavar="LIST",
                   help="比价时查哪些平台，逗号分隔：ly,ctrip,ctrip-public,trip,kiwi,google（默认 all）")
    args = p.parse_args()

    try:
        origin = resolve_city(args.origin)["name"]
        destination = resolve_city(args.destination)["name"]
    except KeyError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1

    rtype = route_type(origin, destination)

    if args.compare:
        return run_compare(args, origin, destination)

    adapters = []
    if args.mode in ("auto", "rail") and rtype == "CN_DOMESTIC":
        adapters.append(Rail12306Adapter())
    if args.mode in ("auto", "rail") and rtype == "VN_DOMESTIC":
        adapters.append(RailVietnamAdapter())
    if args.mode in ("auto", "flight"):
        adapters.append(FlightGoogleAdapter())
    if not adapters:
        print(f"[!] 航线 {origin} -> {destination}（{rtype}）+ 模式 {args.mode} 暂无可用的 adapter。", file=sys.stderr)
        print("    已实现：中国国内高铁、Google Flights 航班。", file=sys.stderr)
        return 2

    query = SearchQuery(origin=origin, destination=destination, date=args.date, mode=args.mode)
    offers = []
    for adapter in adapters:
        try:
            got = adapter.search(query)
            offers.extend(got)
            print(f"[+] {adapter.source_name}: {len(got)} 条", file=sys.stderr)
        except Exception as e:
            print(f"[!] {adapter.source_name} 查询失败：{e}", file=sys.stderr)

    if not offers:
        print("[!] 所有数据源都没有结果（可能未开售、当天无车/无航班或接口被限流）", file=sys.stderr)
        return 1

    if not args.include_soldout:
        offers = [o for o in offers if o.seats_left != 0]

    # 同一来源内按 offer_id 去重，保留最低价
    dedup: dict[str, object] = {}
    for o in offers:
        old = dedup.get(o.offer_id)
        if old is None or o.price.amount_cny < old.price.amount_cny:
            dedup[o.offer_id] = o
    offers = list(dedup.values())

    offers = sort_offers(offers, args.sort)[: args.limit]
    if args.json:
        print(json.dumps([o.to_dict() for o in offers], ensure_ascii=False, indent=2))
    else:
        print(f"\n{origin} → {destination}  {args.date}  |  {rtype}  |  "
              f"共 {len(offers)} 条，按{SORT_NAMES[args.sort]}排序\n")
        print_table(offers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

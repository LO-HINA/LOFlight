"""城市注册表 + 航线类型判定。

用户输入城市名（中文/英文别名），这里负责：
1. 归一化成标准中文名
2. 展开机场（上海 -> SHA/PVG）
3. 判断航线类型，决定调用哪些 adapter
"""
import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "cities.json"

with open(_DATA, encoding="utf-8") as f:
    CITIES = json.load(f)

_ALIASES: dict[str, str] = {}
for _name, _info in CITIES.items():
    _ALIASES[_name] = _name
    for _alias in _info.get("aliases", []):
        _ALIASES[_alias.lower()] = _name

# 机场码 -> 城市码（PEK/PKX -> BJS，HND/NRT -> TYO）。跨平台对齐时统一到城市级：
# 携程按机场给（HND），同程按城市给（TYO），不归一化就永远分不到同一组。
AIRPORT_TO_CITY_CODE: dict[str, str] = {}
CITY_CODE_COUNTRY: dict[str, str] = {}
for _name, _info in CITIES.items():
    _city_code = _info.get("code")
    if _city_code:
        CITY_CODE_COUNTRY[_city_code] = _info["country"]
    for _ap in _info.get("airports", []):
        if _city_code:
            AIRPORT_TO_CITY_CODE[_ap] = _city_code


def city_code_for(code: str) -> str:
    """机场码换成城市码；已经是城市码或没收录的原样返回。"""
    return AIRPORT_TO_CITY_CODE.get(code, code)


def resolve_city(name: str) -> dict:
    key = (name or "").strip()
    if key in CITIES:
        return {"name": key, **CITIES[key]}
    hit = _ALIASES.get(key.lower())
    if hit:
        return {"name": hit, **CITIES[hit]}
    raise KeyError(f"未收录城市：{name}，请在 data/cities.json 里补充")


def route_type(origin: str, destination: str) -> str:
    a = resolve_city(origin)["country"]
    b = resolve_city(destination)["country"]
    if a == b == "CN":
        return "CN_DOMESTIC"
    if a == b == "VN":
        return "VN_DOMESTIC"
    if {a, b} == {"CN", "VN"}:
        return "CN_VN"
    return "INTERNATIONAL"


def adapters_for(origin: str, destination: str, mode: str) -> list[str]:
    """返回该航线 + 交通方式下应调用的 adapter 名称（后续注册更多实现）。"""
    rtype = route_type(origin, destination)
    if mode == "rail":
        if rtype == "CN_DOMESTIC":
            return ["rail_12306"]
        if rtype == "VN_DOMESTIC":
            return ["rail_vn"]
        return []
    if mode == "flight":
        return ["flight_ly", "flight_ctrip", "flight_trip", "flight_kiwi", "flight_google"]
    if mode == "bus":
        return ["bus_12go"]
    return []

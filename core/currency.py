"""币种统一折 CNY。汇率是静态文件，MVP 阶段够用；后续可接汇率 API 每日刷新。"""
import json
from pathlib import Path

_DATA = Path(__file__).resolve().parent.parent / "data" / "exchange_rates.json"


def load_rates() -> dict:
    with open(_DATA, encoding="utf-8") as f:
        return json.load(f)


_RATES = load_rates()


def to_cny(amount: float, currency: str) -> float:
    key = currency.upper()
    rate = _RATES["rates_to_cny"].get(key)
    if rate is None:
        raise ValueError(f"没有 {currency} 的汇率配置，请补充 data/exchange_rates.json")
    return round(amount * rate, 2)


def rate(currency: str) -> float:
    return _RATES["rates_to_cny"][currency.upper()]

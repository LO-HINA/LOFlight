"""Adapter 基类：每个数据源实现一个 search()，输入统一查询，输出统一 TripOffer。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from core.schema import TripOffer


class AdapterError(RuntimeError):
    """数据源不可用 / 返回格式变化时抛出，CLI 负责降级展示。"""


@dataclass
class SearchQuery:
    origin: str                   # 城市中文名，如 上海
    destination: str              # 城市中文名，如 北京
    date: str                     # YYYY-MM-DD
    mode: str = "rail"            # flight / rail / bus
    passengers: int = 1
    cabin: Optional[str] = None   # 经济舱 / 商务座 ...
    return_date: Optional[str] = None


class BaseAdapter(ABC):
    source = ""
    source_name = ""
    supported_modes: set[str] = set()

    @abstractmethod
    def search(self, query: SearchQuery) -> list[TripOffer]:
        """返回该来源下所有可售报价。实现内部自行处理重试和限速。"""
        raise NotImplementedError

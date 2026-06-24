"""
dcSdk — A股行情数据 SDK（独立包）

多数据源自适应：腾讯行情(主力) + 新浪行情(备用) + 东方财富(基本面)
纯 Python 实现，仅依赖 requests。

快速使用:
    from dcsdk import quote, kline, market_summary

    # 个股实时行情
    data = quote("600519")

    # K线数据（日线，最近100条）
    klines = kline("600519", period="day", count=100)

    # 大盘概况
    market = market_summary()

    # 资金流向
    from dcsdk.moneyflow import MoneyFlow
    mf = MoneyFlow()
    flow = mf.stock_detail("600519", market=1)
"""

from .quote import StockQuote
from .market import Market
from .board import Board
from .kline import KLine
from .monitor import LimitMonitor
from .moneyflow import MoneyFlow
from .client import TencentClient, SinaClient, EastMoneyClient

# 函数式 API（开箱即用）
from .api import (
    quote,
    quote_batch,
    market_indexes,
    market_summary,
    kline,
    board_industry,
    board_concept,
    limit_scan,
)

__version__ = "1.0.0"
__all__ = [
    # 类
    "StockQuote", "Market", "Board", "KLine", "LimitMonitor", "MoneyFlow",
    "TencentClient", "SinaClient", "EastMoneyClient",
    # 函数式 API
    "quote", "quote_batch", "market_indexes", "market_summary",
    "kline", "board_industry", "board_concept", "limit_scan",
]

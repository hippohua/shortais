"""
函数式 API 层 — 一行命令即可调用
封装底层实现，提供简洁的接口
"""
from datetime import datetime
from typing import Optional, Dict, List

import requests
import urllib3

from .quote import StockQuote, _code_map, _parse_tencent_quote, _parse_sina_quote
from .market import Market
from .kline import KLine
from .board import Board
from .monitor import LimitMonitor
from .moneyflow import MoneyFlow
from .client import TencentClient, SinaClient, EastMoneyClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _ts() -> str:
    """返回毫秒级时间戳字符串"""
    now = datetime.now()
    return now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"


def _step(steps: list, msg: str, status: str = "ok", detail: str = "") -> None:
    """添加调试步骤记录"""
    steps.append({
        "time": _ts(),
        "step": msg,
        "status": status,
        "detail": detail,
    })


# ======================== 模块实例（全局复用） ========================
_quote = StockQuote()
_market = Market()
_kline = KLine()
_board = Board()
_monitor = LimitMonitor()
_moneyflow = MoneyFlow()
_tencent = TencentClient()
_sina = SinaClient()


# ======================== 数据源信息 ========================
DATA_SOURCES = {
    "tencent": {
        "name": "腾讯行情",
        "domain": "qt.gtimg.cn",
        "protocol": "HTTP/HTTPS",
        "format": "管道符分隔文本 (CSV)",
        "speed": "快",
        "reliability": "高",
    },
    "sina": {
        "name": "新浪行情",
        "domain": "hq.sinajs.cn",
        "protocol": "HTTP/HTTPS",
        "format": "CSV 逗号分隔",
        "speed": "快",
        "reliability": "高",
    },
}


# ======================== 个股行情 ========================

def quote(code: str) -> Dict:
    return _quote.get(code)


def quote_batch(codes: List[str]) -> Dict[str, Dict]:
    return _quote.get_batch(codes)


# ======================== 大盘指数 ========================

def market_indexes(names: Optional[List[str]] = None) -> Dict[str, Dict]:
    return _market.indexes(names)


def market_summary() -> Dict:
    return _market.summary()


# ======================== K线数据 ========================

def kline(code: str, period: str = "day", count: int = 100) -> List[Dict]:
    return _kline.get(code, period, count)


# ======================== 板块排行 ========================

def board_industry(top_n: int = 20) -> List[Dict]:
    """行业板块涨幅排行"""
    return _board.industry_ranking(top_n)


def board_concept(top_n: int = 20) -> List[Dict]:
    """概念板块涨幅排行"""
    return _board.concept_ranking(top_n)


# ======================== 涨跌停监控 ========================

def limit_scan(codes: Optional[List[str]] = None) -> Dict:
    """扫描涨跌停股票"""
    return _monitor.scan(codes)


# ======================== 资金流向 ========================

def trace_moneyflow(code: str) -> Dict:
    """
    获取个股资金流向并记录调用链路

    Args:
        code: 股票代码 "600519"
    """
    steps = []
    _step(steps, f"开始查询资金流向: {code}", "ok")

    market = 1 if str(code).startswith(("6", "9")) else 0
    secid = f"{market}.{code}"

    result = {
        "type": "moneyflow",
        "code": code,
        "steps": steps,
        "source_used": "东方财富 (push2.eastmoney.com)",
        "http_method": "GET",
    }

    try:
        _step(steps, "请求东方财富资金流API", "pending", f"GET stock/get?secid={secid}")
        data = _moneyflow.stock_detail(code, market)
        _step(steps, "资金流数据返回", "ok",
              f"主力净流入: {data.get('main_inflow', '-')} "
              f"(占比 {data.get('main_inflow_pct', '-')}%)")
        result["parsed"] = data
    except Exception as e:
        _step(steps, "资金流查询失败", "error", str(e))
        result["error"] = str(e)

    _step(steps, "返回结果", "ok")
    return result


def trace_northbound() -> Dict:
    """
    获取北向资金（沪深港通）数据并记录调用链路
    """
    steps = []
    _step(steps, "开始查询北向资金", "ok", "沪深港通实时数据")

    result = {
        "type": "northbound",
        "steps": steps,
        "source_used": "东方财富 (push2.eastmoney.com)",
        "http_method": "GET",
    }

    try:
        _step(steps, "请求北向资金API", "pending", "GET push2.eastmoney.com/api/qt/kamt/get")
        data = _moneyflow.north_bound()
        names = {"hk2sh": "沪股通(北向)", "hk2sz": "深股通(北向)",
                 "sh2hk": "港股通(沪)", "sz2hk": "港股通(深)"}
        for key, name in names.items():
            item = data.get(key, {})
            _step(steps, f"解析 {name}", "ok",
                  f"净流入: {item.get('net_inflow', '-')}万")
        result["parsed"] = data
    except Exception as e:
        _step(steps, "北向资金查询失败", "error", str(e))
        result["error"] = str(e)

    _step(steps, "返回结果", "ok")
    return result

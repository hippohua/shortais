"""
大盘行情 — 指数行情 + 涨跌统计
数据源：腾讯行情 (qt.gtimg.cn)
"""
import re
from typing import Optional, Dict, List
from .client import TencentClient


INDEX_MAP = {
    "上证指数": "sh000001",
    "深证成指": "sz399001",
    "创业板指": "sz399006",
    "上证50": "sh000016",
    "科创50": "sh000688",
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "上证180": "sh000010",
}


def _f(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_tencent_index(data: list) -> Dict:
    """解析腾讯指数 CSV"""
    try:
        name = data[1]
        price = _f(data[3])
        pre_close = _f(data[4])
        open_p = _f(data[5])
        volume = _f(data[6])   # 手

        change = _f(data[31]) if len(data) > 31 else None
        change_pct = _f(data[32]) if len(data) > 32 else None
        high = _f(data[33]) if len(data) > 33 else None
        low = _f(data[34]) if len(data) > 34 else None

        # 盘后数据复位自动计算
        if (change == 0 or change_pct == 0) and price and pre_close and pre_close != 0:
            change = round(price - pre_close, 2)
            change_pct = round(change / pre_close * 100, 2)

        amount_raw = data[37] if len(data) > 37 else None
        amount = _f(amount_raw) * 1e4 if amount_raw else None  # 万元→元

        # 涨跌金额 (万元)
        up_amt = _f(data[44]) if len(data) > 44 else None
        down_amt = _f(data[45]) if len(data) > 45 else None

        return {
            "name": name,
            "price": price,
            "pre_close": pre_close,
            "open": open_p,
            "change": change,
            "change_pct": change_pct,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "up_amount": up_amt,
            "down_amount": down_amt,
        }
    except (IndexError, ValueError):
        return {}


class Market:
    """大盘行情"""

    def __init__(self):
        self._client = TencentClient()

    def indexes(self, names: Optional[List[str]] = None) -> Dict[str, Dict]:
        if names is None:
            names = list(INDEX_MAP.keys())
        codes = [INDEX_MAP[n] for n in names if n in INDEX_MAP]
        if not codes:
            return {}

        raw = self._client.get_quotes(codes)
        result = {}
        for line in raw.strip().split("\n"):
            m = re.search(r'"(.+)"', line)
            if not m:
                continue
            data = m.group(1).split("~")
            if len(data) < 35:
                continue
            parsed = _parse_tencent_index(data)
            if parsed and parsed.get("name"):
                result[parsed["name"]] = parsed
        return result

    def summary(self) -> Dict:
        """大盘概览：指数数据 + 涨跌金额统计"""
        idx = self.indexes()
        result = dict(idx)

        sh = idx.get("上证指数", {})
        sz = idx.get("深证成指", {})
        cy = idx.get("创业板指", {})

        result["统计"] = {
            "沪市上涨额": sh.get("up_amount"),
            "沪市下跌额": sh.get("down_amount"),
            "深市上涨额": sz.get("up_amount"),
            "深市下跌额": sz.get("down_amount"),
            "创业板上涨额": cy.get("up_amount"),
            "创业板下跌额": cy.get("down_amount"),
        }
        return result

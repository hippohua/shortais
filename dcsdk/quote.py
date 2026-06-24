"""
个股实时行情 — 精编版
数据源：腾讯行情 (qt.gtimg.cn) 主力 | 新浪行情 (hq.sinajs.cn) 备用
"""
import re
from typing import Optional, Dict, List
from .client import TencentClient, SinaClient


def _f(v) -> Optional[float]:
    """转浮点数"""
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _i(v) -> Optional[int]:
    """转整数"""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _code_map(code: str, market: int = None) -> str:
    """将股票代码转为腾讯/新浪格式 (sh/sz 前缀)"""
    code = str(code)
    # 指数代码优先匹配
    idx_map = {
        "000001": "sh000001", "399001": "sz399001", "399006": "sz399006",
        "000016": "sh000016", "000688": "sh000688", "000300": "sh000300",
        "000905": "sh000905", "000010": "sh000010",
    }
    if code in idx_map:
        return idx_map[code]
    # 常规股票代码
    if code.startswith(("6", "9")):
        return f"sh{code}"
    elif code.startswith(("0", "3")):
        return f"sz{code}"
    elif code.startswith(("8", "4")):
        return f"bj{code}"
    if market == 1:
        return f"sh{code}"
    return f"sz{code}"


def _parse_tencent_quote(line: str) -> Optional[Dict]:
    """解析腾讯行情 CSV 行"""
    if not line or not line.startswith("v_"):
        return None
    try:
        parts = re.search(r'"(.+)"', line)
        if not parts:
            return None
        data = parts.group(1).split("~")
        if len(data) < 50:
            return None

        return {
            "code": data[2],
            "name": data[1],
            "price": _f(data[3]),
            "pre_close": _f(data[4]),
            "open": _f(data[5]),
            "high": _f(data[33]),
            "low": _f(data[34]),
            "volume": _i(data[6]),         # 手
            "amount": _f(data[37]) * 1e4 if data[37] else None,  # 万元→元
            "change": _f(data[31]),
            "change_pct": _f(data[32]),
            "amplitude": _f(data[43]),
            "turnover_rate": _f(data[38]),  # 换手率%
            "total_market_cap": _f(data[44]) * 1e8 if data[44] else None,  # 亿→元
            "float_market_cap": _f(data[45]) * 1e8 if data[45] else None,
            "pe_ratio": _f(data[39]),
            "avg_price": _f(data[51]),
            "limit_up": _f(data[47]),      # 涨停价
            "limit_down": _f(data[48]),    # 跌停价
            "pb_ratio": _f(data[46]),      # 市净率
        }
    except (IndexError, ValueError, AttributeError):
        return None


def _parse_sina_quote(line: str) -> Optional[Dict]:
    """解析新浪行情 CSV 行"""
    if not line or not line.startswith("var hq_str_"):
        return None
    try:
        parts = re.search(r'"(.+)"', line)
        if not parts:
            return None
        data = parts.group(1).split(",")
        if len(data) < 30:
            return None

        name = data[0]
        open_p = _f(data[1])
        pre_close = _f(data[2])
        price = _f(data[3])
        high = _f(data[4])
        low = _f(data[5])
        volume = _i(data[8])   # 手
        amount = _f(data[9])   # 元

        return {
            "name": name,
            "price": price,
            "pre_close": pre_close,
            "open": open_p,
            "high": high,
            "low": low,
            "volume": volume,
            "amount": amount,
            "change": round(price - pre_close, 2) if price and pre_close else None,
            "change_pct": round((price - pre_close) / pre_close * 100, 2)
                         if price and pre_close and pre_close != 0 else None,
        }
    except (IndexError, ValueError, AttributeError):
        return None


class StockQuote:
    """个股实时行情查询"""

    def __init__(self):
        self._tencent = TencentClient()
        self._sina = SinaClient()

    def get(self, code: str, market: int = None) -> Dict:
        """
        获取个股实时行情

        Args:
            code: 股票代码 "600519" / "300750"
            market: 市场 (1=沪, 0=深), None=自动识别

        Returns:
            {"name": "贵州茅台", "price": 1215.0, "change_pct": -2.02, ...}
        """
        tc = _code_map(code, market)

        # 优先腾讯
        try:
            raw = self._tencent.get_quotes([tc])
            for line in raw.strip().split("\n"):
                result = _parse_tencent_quote(line)
                if result:
                    result["code"] = code
                    return result
        except Exception:
            pass

        # 备用新浪
        try:
            raw = self._sina.get_quotes([tc])
            for line in raw.strip().split("\n"):
                result = _parse_sina_quote(line)
                if result:
                    result["code"] = code
                    return result
        except Exception:
            pass

        return {"code": code, "error": "所有数据源均不可用"}

    def get_batch(self, codes: List[str]) -> Dict[str, Dict]:
        """
        批量获取行情

        Args:
            codes: ["600519", "300750"]

        Returns:
            {"600519": {...}, "300750": {...}}
        """
        tc = [_code_map(c) for c in codes]

        result = {}
        try:
            raw = self._tencent.get_quotes(tc)
            for line in raw.strip().split("\n"):
                r = _parse_tencent_quote(line)
                if r:
                    result[r["code"]] = r
        except Exception:
            pass

        return result

    @staticmethod
    def _code_map(code, market=None):
        return _code_map(code, market)

"""
K线数据 — 基于腾讯行情
"""
from typing import Optional, Dict, List
from .client import BaseClient


class KLine:
    """K线数据"""

    PERIOD_MAP = {
        "day": "day",
        "week": "week",
        "month": "month",
    }

    def __init__(self):
        self._client = BaseClient()

    def get(self, code: str, period: str = "day",
            count: int = 100, market: int = None) -> List[Dict]:
        """
        获取 K 线数据 (前复权)

        Args:
            code: 股票代码 "600519"
            period: "day" / "week" / "month"
            count: 返回条数
            market: 市场 (1=沪, 0=深)

        Returns:
            [{ "date": "2026-06-18", "open": xx, "close": xx,
               "high": xx, "low": xx, "volume": xx }, ...]
        """
        tc = self._code_map(code, market)
        period_key = self.PERIOD_MAP.get(period, "day")

        url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        params = {
            "param": f"{tc},{period_key},,,{count},qfq",
        }

        try:
            r = self._client.get(
                url, params=params,
                headers={"Referer": "https://gu.qq.com/"},
            )
            data = r.json()
            return self._parse(data, tc, period_key)
        except Exception:
            pass
        return []

    def _parse(self, data: Dict, code: str, period: str) -> List[Dict]:
        """解析腾讯 K 线响应"""
        result = []
        try:
            stock_data = data.get("data", {}).get(code, {})
            # 腾讯返回 qfqday, qfqweek, qfqmonth
            key = f"qfq{period}"
            klines = stock_data.get(key, [])

            for item in klines:
                if len(item) >= 6:
                    result.append({
                        "date": str(item[0])[:10],
                        "open": float(item[1]),
                        "close": float(item[2]),
                        "high": float(item[3]),
                        "low": float(item[4]),
                        "volume": float(item[5]),
                    })
        except (KeyError, IndexError, ValueError, TypeError):
            pass
        return result

    @staticmethod
    def _code_map(code: str, market: int = None) -> str:
        code = str(code)
        if code.startswith(("6", "9")):
            return f"sh{code}"
        elif code.startswith(("0", "3")):
            return f"sz{code}"
        if market == 1:
            return f"sh{code}"
        return f"sz{code}"

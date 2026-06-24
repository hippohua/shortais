"""
资金流向 — 个股/板块资金流、沪深港通
"""
from typing import Optional, Dict
from .client import EastMoneyClient


class MoneyFlow:
    """资金流向数据"""

    def __init__(self, client: Optional[EastMoneyClient] = None):
        self.client = client or EastMoneyClient()

    def stock_detail(self, code: str, market: int = 1) -> Dict:
        """
        个股资金流向详情

        Returns:
            {
                "main_inflow": 主力净流入,
                "main_inflow_pct": 主力净占比,
                "super_inflow": 超大单净流入,
                "big_inflow": 大单净流入,
                "mid_inflow": 中单净流入,
                "small_inflow": 小单净流入,
            }
        """
        fields = (
            "f135,f136,f137,f138,f139,f140,f141,f142,f143,"
            "f144,f145,f146,f147,f148,f149,f150,f151"
        )
        secid = f"{market}.{code}"
        raw = self.client.get_stock(secid, fields)
        d = raw.get("data", {})

        return {
            "main_inflow": d.get("f136"),
            "main_inflow_pct": d.get("f147"),   # 主力净占比(%)
            "super_inflow": d.get("f138"),       # 超大单
            "big_inflow": d.get("f141"),         # 大单
            "mid_inflow": d.get("f144"),         # 中单
            "small_inflow": d.get("f146"),       # 小单
            "super_inflow_pct": d.get("f148"),
            "big_inflow_pct": d.get("f149"),
            "mid_inflow_pct": d.get("f150"),
            "small_inflow_pct": d.get("f151"),
        }

    def north_bound(self) -> Dict:
        """
        沪深港通（北向资金）数据
        """
        fields1 = "f1,f2,f3,f4,f55"
        fields2 = "f51,f52,f53,f54,f56,f60,f61,f62,f63,f65,f66"

        params = {
            "fields1": fields1,
            "fields2": fields2,
            "ut": "13697a1cc677c8bfa9a496437bfef419",
        }
        raw = self.client._request(
            "https://push2.eastmoney.com/api/qt/kamt/get", params
        )
        data = raw.get("data", {})
        result = {}
        for key in ("hk2sh", "hk2sz", "sh2hk", "sz2hk"):
            item = data.get(key, {})
            result[key] = {
                "net_inflow": item.get("dayNetAmtIn"),
                "buy_amt": item.get("buyAmt"),
                "sell_amt": item.get("sellAmt"),
                "net_buy_amt": item.get("netBuyAmt"),
                "status": item.get("status"),
            }
        return result

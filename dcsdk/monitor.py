"""
涨停/跌停监控 — 基于腾讯行情实时数据
个股涨跌停判断：当前价格 >= 涨停价 或 当前价格 <= 跌停价
"""
from datetime import datetime
from typing import Optional, Dict, List
from .client import TencentClient
from .quote import _code_map, _parse_tencent_quote


# 默认监控股票池 (沪深300 热门股票 + 各行业龙头)
DEFAULT_WATCH_LIST = [
    "600519",  # 贵州茅台 (消费)
    "300750",  # 宁德时代 (新能源)
    "601318",  # 中国平安 (金融)
    "000858",  # 五粮液 (消费)
    "002415",  # 海康威视 (科技)
    "600036",  # 招商银行 (金融)
    "000333",  # 美的集团 (家电)
    "601166",  # 兴业银行 (金融)
    "600900",  # 长江电力 (公用)
    "002594",  # 比亚迪 (新能源)
    "601012",  # 隆基绿能 (新能源)
    "300059",  # 东方财富 (金融)
    "600276",  # 恒瑞医药 (医药)
    "002475",  # 立讯精密 (电子)
    "688981",  # 中芯国际 (半导体)
    "000725",  # 京东方A (电子)
    "601899",  # 紫金矿业 (有色)
    "600030",  # 中信证券 (金融)
    "601728",  # 中国电信 (通信)
    "600887",  # 伊利股份 (消费)
    "002230",  # 科大讯飞 (AI)
    "300308",  # 中际旭创 (通信)
    "601857",  # 中国石油 (能源)
    "600809",  # 山西汾酒 (消费)
    "002714",  # 牧原股份 (农牧)
    "000568",  # 泸州老窖 (消费)
    "601088",  # 中国神华 (能源)
    "600028",  # 中国石化 (能源)
    "688111",  # 金山办公 (软件)
    "300124",  # 汇川技术 (工业)
]


class LimitMonitor:
    """
    涨跌停监控

    使用方法:
        lm = LimitMonitor()
        result = lm.scan()  # 扫描默认股票池
        # 或自定义股票池
        result = lm.scan(codes=["600519", "300750"])
    """

    def __init__(self):
        self._client = TencentClient()

    def scan(self, codes: Optional[List[str]] = None) -> Dict:
        """
        扫描股票池，返回涨跌停统计

        Args:
            codes: 股票代码列表，默认使用 DEFAULT_WATCH_LIST

        Returns:
            {
                "total": 30,
                "limit_up": [...],
                "limit_down": [...],
                "near_limit_up": [...],
                "near_limit_down": [...],
                "normal": [...],
                "timestamp": "2026-06-19 15:00:00",
            }
        """
        if codes is None:
            codes = DEFAULT_WATCH_LIST

        tc = [_code_map(c) for c in codes]

        try:
            raw = self._client.get_quotes(tc)
        except Exception as e:
            return {"error": f"数据请求失败: {e}"}

        limit_up = []
        limit_down = []
        near_limit_up = []
        near_limit_down = []
        normal = []
        for line in raw.strip().split("\n"):
            parsed = _parse_tencent_quote(line)
            if not parsed:
                continue

            price = parsed.get("price")
            limit_up_price = parsed.get("limit_up")
            limit_down_price = parsed.get("limit_down")
            change_pct = parsed.get("change_pct")

            item = {
                "code": parsed.get("code", ""),
                "name": parsed.get("name", ""),
                "price": price,
                "change_pct": change_pct,
                "limit_up": limit_up_price,
                "limit_down": limit_down_price,
                "pre_close": parsed.get("pre_close"),
            }

            # 判断涨跌停
            is_limit_up = (
                price is not None and limit_up_price is not None
                and abs(price - limit_up_price) < 0.01
            )
            is_limit_down = (
                price is not None and limit_down_price is not None
                and abs(price - limit_down_price) < 0.01
            )

            if is_limit_up:
                limit_up.append(item)
            elif is_limit_down:
                limit_down.append(item)
            elif change_pct is not None and change_pct >= 9.5:
                near_limit_up.append(item)
            elif change_pct is not None and change_pct <= -9.5:
                near_limit_down.append(item)
            else:
                normal.append(item)

        return {
            "total": len(limit_up) + len(limit_down) + len(near_limit_up) + len(near_limit_down) + len(normal),
            "limit_up": limit_up,
            "limit_down": limit_down,
            "near_limit_up": near_limit_up,
            "near_limit_down": near_limit_down,
            "normal": normal,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def get_default_watch_list() -> List[str]:
        """获取默认监控股票池"""
        return list(DEFAULT_WATCH_LIST)

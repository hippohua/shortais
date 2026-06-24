"""
板块热度排行 — 基于深交所行业指数 + 腾讯行情
数据源：腾讯行情 (qt.gtimg.cn) — 行业指数查询
"""
import re
from typing import Optional, Dict, List
from .client import TencentClient


# 深交所行业板块指数
INDUSTRY_BOARDS = {
    "金融指数": "sz399240",
    "地产指数": "sz399241",
    "农林指数": "sz399231",
    "采矿指数": "sz399232",
    "制造指数": "sz399233",
    "水电指数": "sz399234",
    "建筑指数": "sz399235",
    "批零指数": "sz399236",
    "运输指数": "sz399237",
    "IT指数": "sz399239",
    "商务指数": "sz399242",
    "科研指数": "sz399243",
    "公共指数": "sz399244",
    "文化指数": "sz399248",
    "碳科技30": "sz399030",
    "碳科技60": "sz399060",
}

CONCEPT_BOARDS = {
    "创新引擎": "sz399050",
    "深市精选": "sz399013",
    "深证创新": "sz399016",
    "SME创新": "sz399017",
    "创业创新": "sz399018",
    "中小创新": "sz399015",
}


def _f(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _parse_board_quote(data: list) -> Optional[Dict]:
    """解析板块指数行情 (格式同指数)"""
    try:
        name = data[1]
        price = _f(data[3])
        pre_close = _f(data[4])
        change = _f(data[31]) if len(data) > 31 else None
        change_pct = _f(data[32]) if len(data) > 32 else None
        high = _f(data[33]) if len(data) > 33 else None
        low = _f(data[34]) if len(data) > 34 else None
        volume = _f(data[6])   # 手

        # 盘后复位自动计算
        if (change == 0 or change_pct == 0) and price and pre_close and pre_close != 0:
            change = round(price - pre_close, 2)
            change_pct = round(change / pre_close * 100, 2)

        return {
            "name": name,
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "high": high,
            "low": low,
            "volume": volume,
        }
    except (IndexError, ValueError):
        return None


class Board:
    """板块排行数据"""

    def __init__(self):
        self._client = TencentClient()

    def industry_ranking(self, top_n: int = 20) -> List[Dict]:
        """
        行业板块涨幅排行

        Args:
            top_n: 返回前 N 个板块

        Returns:
            [{"name": "金融指数", "code": "sz399240", "change_pct": 1.23, ...}, ...]
        """
        return self._ranking(INDUSTRY_BOARDS, top_n)

    def concept_ranking(self, top_n: int = 20) -> List[Dict]:
        """
        概念板块涨幅排行

        Args:
            top_n: 返回前 N 个概念板块

        Returns:
            [{"name": "创新引擎", "code": "sz399050", "change_pct": 1.23, ...}, ...]
        """
        return self._ranking(CONCEPT_BOARDS, top_n)

    def _ranking(self, board_map: Dict[str, str], top_n: int) -> List[Dict]:
        """通用板块排行查询"""
        if not board_map:
            return []

        names = list(board_map.keys())
        codes = [board_map[n] for n in names]

        # 建立 短代码→(名称, 完整代码) 的查找表
        code_to_name = {v[-6:]: (k, v) for k, v in board_map.items()}

        try:
            raw = self._client.get_quotes(codes)
            results = []
            for line in raw.strip().split("\n"):
                m = re.search(r'"(.+)"', line)
                if not m:
                    continue
                data = m.group(1).split("~")
                if len(data) < 35:
                    continue
                short_code = data[2]
                match = code_to_name.get(short_code)
                if not match:
                    continue
                parsed = _parse_board_quote(data)
                if parsed:
                    parsed["name"] = match[0]
                    parsed["code"] = match[1]
                    results.append(parsed)

            # 按涨跌幅排序 (降序)
            results.sort(key=lambda x: x.get("change_pct") or 0, reverse=True)
            return results[:top_n]
        except Exception:
            return []

    @staticmethod
    def supported_sources() -> List[str]:
        """返回支持的板块数据源"""
        return ["tencent"]

    @staticmethod
    def available_industry_boards() -> Dict[str, str]:
        """返回所有可查询的行业板块"""
        return dict(INDUSTRY_BOARDS)

    @staticmethod
    def available_concept_boards() -> Dict[str, str]:
        """返回所有可查询的概念板块"""
        return dict(CONCEPT_BOARDS)

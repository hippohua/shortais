"""
HTTP 客户端封装 — 多数据源自适应
支持 Tencent Finance（主力）、Sina Finance（备用）、EastMoney（基本面）
"""
from typing import Optional, Dict, List

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class BaseClient:
    """通用 HTTP 客户端"""

    def __init__(self, timeout: int = 10):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        })
        self.timeout = timeout

    def get(self, url: str, params: Optional[Dict] = None, **kwargs) -> requests.Response:
        return self.session.get(
            url, params=params, timeout=self.timeout, verify=False, **kwargs
        )


class TencentClient(BaseClient):
    """腾讯行情 API 客户端 (qt.gtimg.cn) — 最可靠的数据源"""

    BASE = "https://qt.gtimg.cn"

    def get_quotes(self, codes: List[str]) -> str:
        """批量获取实时行情
        codes: ['sh600519', 'sz300750', 'sh000001']
        """
        q = ",".join(codes)
        r = self.get(f"{self.BASE}/q={q}", headers={"Referer": "https://gu.qq.com/"})
        r.encoding = "gbk"
        return r.text


class SinaClient(BaseClient):
    """新浪行情 API 客户端 (hq.sinajs.cn) — 备用数据源"""

    BASE = "https://hq.sinajs.cn"

    def get_quotes(self, codes: List[str]) -> str:
        """批量获取实时行情
        codes: ['sh600519', 'sz300750', 'sh000001']
        """
        q = ",".join(codes)
        r = self.get(
            f"{self.BASE}/list={q}",
            headers={"Referer": "https://finance.sina.com.cn"},
        )
        r.encoding = "gbk"
        return r.text


class EastMoneyClient(BaseClient):
    """东方财富数据接口客户端 (datacenter-web) — 基本面数据"""

    DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    QUOTE_BASE = "https://push2.eastmoney.com/api/qt/stock/get"

    def get_stock(self, secid: str, fields: str) -> Dict:
        """获取个股实时数据（东方财富行情接口）"""
        params = {
            "secid": secid,
            "fields": fields,
            "ut": "fa5fd1943c7b386f172d6893dbf28fa1",
        }
        r = self.get(self.QUOTE_BASE, params=params,
                     headers={"Referer": "https://quote.eastmoney.com/"})
        return r.json()

    def _request(self, url: str, params: Dict) -> Dict:
        """通用 JSON 请求"""
        r = self.get(url, params=params,
                     headers={"Referer": "https://data.eastmoney.com/"})
        return r.json()

    def get_datacenter(self, report_name: str, columns: str,
                       page: int = 1, size: int = 20,
                       sort: str = "-1", sort_col: str = "",
                       filter_str: str = "") -> Dict:
        """获取数据中心数据"""
        params = {
            "reportName": report_name,
            "columns": columns,
            "pageNumber": page,
            "pageSize": size,
            "source": "QuoteWeb",
            "client": "WEB",
        }
        if sort_col:
            params["sortTypes"] = sort
            params["sortColumns"] = sort_col
        if filter_str:
            params["filter"] = filter_str

        r = self.get(self.DATACENTER, params=params,
                     headers={"Referer": "https://data.eastmoney.com/"})
        return r.json()

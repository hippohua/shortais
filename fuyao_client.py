"""
同花顺扶摇金融数据API客户端
API文档: https://fuyao.aicubes.cn/docs/
Base URL: https://fuyao.aicubes.cn
鉴权: Header X-api-key

核心接口：
  - 行情快照: GET /api/a-share/prices/snapshot
  - 历史K线:  GET /api/a-share/prices/historical
  - 标的检索: GET /api/meta/tickers/search
  - 标的列表: GET /api/meta/tickers/list
  - 指数列表: GET /api/a-share-index/catalog/ths-index-list
  - 指数成分股: GET /api/a-share-index/constituents/ths-stock-list
  - 指数行情: GET /api/a-share-index/prices/snapshot
"""
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
import warnings

warnings.filterwarnings('ignore')

try:
    import urllib3
    urllib3.disable_warnings()
except Exception:
    pass

from config import FUYAO_API_KEY, FUYAO_BASE_URL

# ==================== 请求辅助 ====================

def _headers() -> dict:
    return {
        'X-api-key': FUYAO_API_KEY,
        'Content-Type': 'application/json',
    }


def _get(endpoint: str, params: Optional[dict] = None, max_retries: int = 2) -> dict:
    """统一GET请求，带重试"""
    url = f"{FUYAO_BASE_URL}{endpoint}"
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params, timeout=30, verify=False)
            data = resp.json()
            if data.get('code') == 0:
                return data.get('data', {})
            elif data.get('code') == 4001:  # 频率超限
                time.sleep(3 * (attempt + 1))
                continue
            else:
                last_err = f"API error code={data.get('code')}: {data.get('message')}"
                if attempt < max_retries:
                    time.sleep(2)
                    continue
        except requests.Timeout:
            last_err = "请求超时"
            if attempt < max_retries:
                time.sleep(3)
                continue
        except Exception as e:
            last_err = str(e)
            if attempt < max_retries:
                time.sleep(2)
                continue

    if last_err:
        print(f"  [FUYAO] {endpoint} 失败: {last_err}")
    return {}


def _parse_date_to_ms(date_str: str, end_of_day: bool = True) -> int:
    """将 YYYYMMDD 转为毫秒时间戳（Asia/Shanghai）"""
    from datetime import timezone, timedelta as td
    dt = datetime.strptime(date_str, '%Y%m%d')
    if end_of_day:
        dt = dt.replace(hour=15, minute=0, second=0)  # A股收盘时间
    else:
        dt = dt.replace(hour=9, minute=30, second=0)  # A股开盘时间
    tz_shanghai = timezone(td(hours=8))
    dt = dt.replace(tzinfo=tz_shanghai)
    return int(dt.timestamp() * 1000)


# ==================== 股票列表 ====================

def get_all_a_stock_codes() -> list[str]:
    """
    获取全部A股6位代码列表。
    通过标的列表分页接口循环获取。
    """
    all_codes = []
    offset = 0
    limit = 5000  # 单次最大10000，保守取5000
    page = 1
    while True:
        data = _get('/api/meta/tickers/list', params={
            'asset_type': 'a-share',
            'limit': limit,
            'offset': offset,
        })
        items = data.get('item', [])
        if not items:
            break
        for item in items:
            ticker = item.get('ticker', '')
            if ticker and len(ticker) == 6:
                all_codes.append(ticker)
        if len(items) < limit:
            break
        offset += limit
        page += 1
        time.sleep(0.2)
        if page % 5 == 0:
            print(f"    已获取 {len(all_codes)} 只...")

    print(f"  [FUYAO] 获取到 {len(all_codes)} 只A股代码")
    return all_codes


def search_ticker(keyword: str, asset_type: str = 'a-share', limit: int = 10) -> list[dict]:
    """标的检索：按名称/代码搜索"""
    data = _get('/api/meta/tickers/search', params={
        'q': keyword,
        'asset_type': asset_type,
        'limit': min(limit, 50),
    })
    return data.get('item', [])


# ==================== 行情快照 ====================

def fetch_snapshot(thscodes: list[str]) -> list[dict]:
    """
    获取行情快照（单只或批量）。
    thscodes: 如 ['600519.SH', '000001.SZ']
    返回: 原始 item 列表
    """
    if not thscodes:
        return []
    codes_str = ','.join(thscodes)
    data = _get('/api/a-share/prices/snapshot', params={'thscodes': codes_str})
    return data.get('item', [])


def fetch_snapshot_all(limit: int = 5000) -> list[dict]:
    """获取全市场行情快照（分页）"""
    all_items = []
    offset = 0
    while True:
        data = _get('/api/a-share/prices/snapshot', params={
            'limit': limit,
            'offset': offset,
        })
        items = data.get('item', [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)
    return all_items


def fetch_quotes_batch(codes: list[str]) -> dict[str, dict]:
    """
    批量获取行情，返回 {code: {name, close, pct_change, ...}}
    每次最多100只（避免URL过长）
    """
    result = {}
    batch_size = 80
    total = len(codes)

    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        # 6位代码 → thscode（加后缀）
        thscodes = []
        for c in batch:
            c = str(c).zfill(6)
            if c.startswith(('6', '9')):
                thscodes.append(f'{c}.SH')
            else:
                thscodes.append(f'{c}.SZ')

        items = fetch_snapshot(thscodes)
        for item in items:
            ticker = item.get('ticker', '')
            result[ticker] = {
                'code': ticker,
                'name': '',  # 快照不含名称，需后续补充
                'close': float(item.get('last_price', 0) or 0),
                'pct_change': float(item.get('price_change_ratio_pct', 0) or 0),
                'volume_amount': float(item.get('turnover', 0) or 0),  # 成交额(元)
                'volume_shares': float(item.get('volume', 0) or 0),     # 成交量(股)
                'high': float(item.get('high_price', 0) or 0),
                'low': float(item.get('low_price', 0) or 0),
                'open': float(item.get('open_price', 0) or 0),
                'pre_close': float(item.get('prev_price', 0) or 0),
            }
        time.sleep(0.15)

    # 批量补名称：通过标的检索
    _batch_fill_names(result)

    return result


def _batch_fill_names(quotes: dict[str, dict]):
    """为行情数据批量补充股票名称"""
    need_name = [c for c, q in quotes.items() if not q.get('name')]
    if not need_name:
        return

    for code in need_name:
        try:
            results = search_ticker(code, limit=1)
            if results:
                quotes[code]['name'] = results[0].get('name', '')
        except Exception:
            pass
        time.sleep(0.05)


# ==================== 历史K线 ====================

def fetch_kline(code: str, days: int = 60, end_date: Optional[str] = None,
                adjust: str = 'forward') -> pd.DataFrame:
    """
    获取单只股票历史日K线。
    code: 6位代码
    days: 回看天数
    end_date: 结束日期 YYYYMMDD（默认今天）
    adjust: forward(前复权)/backward(后复权)/none
    返回: DataFrame（日期/开盘/收盘/最高/最低/成交量/成交额）
    """
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')

    # 构建 thscode
    c = str(code).zfill(6)
    if c.startswith(('6', '9')):
        thscode = f'{c}.SH'
    else:
        thscode = f'{c}.SZ'

    end_ms = _parse_date_to_ms(end_date, end_of_day=True)
    # 开始时间：多取一些天数避免休市导致不足
    start_date = datetime.strptime(end_date, '%Y%m%d') - timedelta(days=days * 3)
    start_ms = _parse_date_to_ms(start_date.strftime('%Y%m%d'), end_of_day=False)

    all_items = []
    offset = 0
    limit = 500

    while True:
        params = {
            'thscode': thscode,
            'interval': '1d',
            'start': start_ms,
            'end': end_ms,
            'adjust': adjust,
            'limit': limit,
            'offset': offset,
        }
        data = _get('/api/a-share/prices/historical', params=params)
        items = data.get('item', [])
        if not items:
            break
        all_items.extend(items)
        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.1)

    if not all_items:
        return pd.DataFrame()

    # 转为 DataFrame
    records = []
    for item in all_items:
        date_ms = item.get('date_ms', 0)
        dt = datetime.fromtimestamp(date_ms / 1000)
        records.append({
            '日期': dt.strftime('%Y-%m-%d'),
            '开盘': float(item.get('open_price', 0) or 0),
            '收盘': float(item.get('close_price', 0) or 0),
            '最高': float(item.get('high_price', 0) or 0),
            '最低': float(item.get('low_price', 0) or 0),
            '成交量': float(item.get('volume', 0) or 0),
            '成交额': float(item.get('turnover', 0) or 0),
        })

    df = pd.DataFrame(records)
    df = df.sort_values('日期').tail(days).reset_index(drop=True)
    return df


def fetch_kline_batch(codes: list[str], days: int = 60,
                      end_date: Optional[str] = None) -> dict[str, pd.DataFrame]:
    """批量获取K线，返回 {code: DataFrame}"""
    result = {}
    total = len(codes)
    for idx, code in enumerate(codes):
        try:
            df = fetch_kline(code, days=days, end_date=end_date)
            if not df.empty:
                result[str(code).zfill(6)] = df
        except Exception as e:
            print(f"    [FUYAO] K线 {code} 失败: {e}")
        # 每10只打印进度
        if (idx + 1) % 10 == 0:
            print(f"    K线进度: {idx+1}/{total}")
        time.sleep(0.08)
    return result


# ==================== 板块指数 ====================

def fetch_industry_boards(tag: str = 'industry') -> list[dict]:
    """获取同花顺行业板块列表"""
    data = _get('/api/a-share-index/catalog/ths-index-list', params={'tag': tag})
    return data.get('item', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])


def fetch_concept_boards(tag: str = 'cn_concept') -> list[dict]:
    """获取同花顺概念板块列表"""
    return fetch_industry_boards(tag=tag)


def fetch_board_constituents(thscode: str) -> list[dict]:
    """获取板块/指数成分股"""
    data = _get('/api/a-share-index/constituents/ths-stock-list', params={'thscode': thscode})
    return data.get('item', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])


def fetch_board_snapshot(thscodes: list[str]) -> list[dict]:
    """获取指数行情快照"""
    if not thscodes:
        return []
    codes_str = ','.join(thscodes)
    data = _get('/api/a-share-index/prices/snapshot', params={'thscodes': codes_str})
    return data.get('item', [])


# ==================== 市场概览（模拟大盘行情） ====================

def fetch_market_summary() -> dict:
    """获取大盘主要指数行情"""
    indices = ['000001.SH', '399001.SZ', '399006.SZ', '000688.SH']  # 上证/深证/创业板/科创50
    names = {'000001.SH': '上证指数', '399001.SZ': '深证成指', '399006.SZ': '创业板指', '000688.SH': '科创50'}
    items = fetch_board_snapshot(indices)
    result = {}
    for item in items:
        code = item.get('thscode', '')
        result[code] = {
            'name': names.get(code, ''),
            'close': float(item.get('last_price', 0) or 0),
            'pct_change': float(item.get('price_change_ratio_pct', 0) or 0),
            'volume_amount': float(item.get('turnover', 0) or 0),
        }
    return result


# ==================== 测试入口 ====================

if __name__ == '__main__':
    print("=== 同花顺扶摇API测试 ===\n")

    # 1. 测试标的检索
    print("1. 标的检索: 贵州茅台")
    results = search_ticker('贵州茅台')
    for r in results:
        print(f"   {r['thscode']} {r['name']}")
    print()

    # 2. 测试行情快照
    print("2. 行情快照: 600519.SH")
    snapshot = fetch_snapshot(['600519.SH'])
    for s in snapshot:
        print(f"   {s['ticker']} 最新价={s['last_price']} 涨跌幅={s['price_change_ratio_pct']}%")
    print()

    # 3. 测试K线
    print("3. 历史K线: 600519 近10日")
    kline = fetch_kline('600519', days=10)
    if not kline.empty:
        print(kline.tail(3).to_string(index=False))
    print()

    # 4. 测试市场概览
    print("4. 市场概览:")
    summary = fetch_market_summary()
    for k, v in summary.items():
        print(f"   {v['name']}: {v['close']} ({v['pct_change']:+.2f}%)")

"""
短线助手 — 数据获取模块
数据源策略（多级回退）：
  首选: 东方财富 datacenter（股票列表） + 腾讯 qt.gtimg.cn（批量行情）
        → 按成交额排序 → VOLUME_TOP_N
        → 按换手率×涨跌幅绝对值 排序 → HOT_TOP_N
  备1:  pywencai 自然语言选股（成交额排名 + 热度排名）
  备2:  同花顺热点接口 → 强势股 + 人工标注题材标签
  兜底: akshare → 仅作兜底（行情接口底层打东财，有反爬风险）

输出: data/{date}/raw_data.xlsx（含"成交额排名"和"热度排名"两个sheet）
"""
import pandas as pd
from datetime import datetime
import os
import time
import re
import json
import warnings
import requests
import urllib3
warnings.filterwarnings('ignore')
urllib3.disable_warnings()

# ---- dcsdk（K线/大盘/板块首选） ----
try:
    from dcsdk import quote, quote_batch, kline, market_summary, board_industry, board_concept, limit_scan
    DCSDK_AVAILABLE = True
except Exception:
    DCSDK_AVAILABLE = False

# ---- pywencai（备用选股源） ----
try:
    import pywencai
    PYWENCAI_AVAILABLE = True
except Exception:
    PYWENCAI_AVAILABLE = False

# ---- akshare（兜底） ----
try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except Exception:
    AKSHARE_AVAILABLE = False

from config import VOLUME_TOP_N, HOT_TOP_N, OUTPUT_DIR

RETRY_MAX = 3
RETRY_DELAY = 2  # 秒

# ==================== 股票代码缓存 ====================
_STOCK_CODE_CACHE_FILE = os.path.join(OUTPUT_DIR, "stock_codes_cache.json")


def _get_all_a_stock_codes() -> list:
    """
    从东方财富 datacenter 获取全部A股代码，带本地缓存（24小时有效）。
    返回: 6位代码列表，如 ['000001','000002',...]
    """
    # 1. 读缓存
    if os.path.exists(_STOCK_CODE_CACHE_FILE):
        try:
            with open(_STOCK_CODE_CACHE_FILE, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            cached_time = cache.get('ts', 0)
            codes = cache.get('codes', [])
            # 24小时内有效
            if codes and time.time() - cached_time < 86400:
                return codes
        except Exception:
            pass

    # 2. 从东方财富 datacenter 获取
    print("  [INFO] 从东方财富 datacenter 获取全部A股代码...")
    codes_set = set()
    page = 1
    page_size = 500

    while True:
        params = {
            'reportName': 'RPT_LICO_FN_CPD',
            'columns': 'SECURITY_CODE,SECURITY_NAME_ABBR',
            'pageNumber': page,
            'pageSize': page_size,
            'source': 'WEB',
            'client': 'WEB',
            'sortTypes': '1',
            'sortColumns': 'SECURITY_CODE',
        }
        try:
            r = requests.get(
                'https://datacenter-web.eastmoney.com/api/data/v1/get',
                params=params,
                headers={'Referer': 'https://data.eastmoney.com/'},
                timeout=20, verify=False
            )
            d = r.json()
            if not d.get('success') or not d.get('result'):
                break
            items = d['result'].get('data', [])
            if not items:
                break
            for item in items:
                code = item.get('SECURITY_CODE', '')
                if code:
                    codes_set.add(code)
            if len(items) < page_size:
                break
            page += 1
            time.sleep(0.2)
        except Exception as e:
            print(f"  [WARN] datacenter page {page} 获取失败: {e}")
            break

    codes = sorted(codes_set)
    print(f"  [OK] 获取到 {len(codes)} 只A股代码")

    # 3. 写缓存
    if codes:
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            with open(_STOCK_CODE_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'ts': time.time(), 'codes': codes}, f, ensure_ascii=False)
        except Exception:
            pass

    return codes


def _fetch_tencent_batch_quotes(codes: list, batch_size: int = 80) -> dict:
    """
    通过腾讯 qt.gtimg.cn 批量获取实时行情。
    返回: {code: {name, price, pct_change, volume_amount, turnover_rate, ...}}
    """
    result = {}
    total = len(codes)

    for i in range(0, total, batch_size):
        batch = codes[i:i + batch_size]
        # 构建腾讯格式代码：6开头→sh，0/3开头→sz
        tc_codes = []
        for c in batch:
            if c.startswith(('6', '9')):
                tc_codes.append(f'sh{c}')
            else:
                tc_codes.append(f'sz{c}')

        try:
            url = 'https://qt.gtimg.cn/q=' + ','.join(tc_codes)
            r = requests.get(url, headers={'Referer': 'https://gu.qq.com/'}, timeout=15, verify=False)
            r.encoding = 'gbk'
            lines = [l for l in r.text.strip().split('\n') if l.startswith('v_')]

            for line in lines:
                try:
                    # 腾讯行情格式: v_sh600519="1~贵州茅台~600519~1850.00~..."
                    # 字段: 0=未知,1=名称,2=代码,3=当前价,4=昨收,5=开盘,6=成交量(手),7=外盘,8=内盘,
                    #        9=买一,10=买一量,...,29=日期,30=时间,31=涨跌额,32=涨跌幅,33=最高,
                    #        34=最低,35=价格/成交量/成交额,36=成交量(手),37=成交额(万),38=换手率,...
                    # 实际字段可能因交易所不同有偏移
                    line = line.strip()
                    if '="' not in line:
                        continue
                    content = line.split('="', 1)[1].rstrip('";\n')
                    fields = content.split('~')
                    if len(fields) < 40:
                        continue

                    code = fields[2]
                    name = fields[1]
                    price = fields[3]
                    pct_change = fields[32]
                    volume_amount = fields[37]  # 成交额(万)
                    turnover_rate = fields[38]  # 换手率
                    high = fields[33]
                    low = fields[34]
                    open_price = fields[5]
                    pre_close = fields[4]

                    result[code] = {
                        'code': code,
                        'name': name,
                        'close': float(price) if price and price != '' else 0,
                        'pct_change': float(pct_change) if pct_change and pct_change != '' else 0,
                        'volume_amount': float(volume_amount) if volume_amount and volume_amount != '' else 0,
                        'turnover_rate': float(turnover_rate) if turnover_rate and turnover_rate != '' else 0,
                        'high': float(high) if high and high != '' else 0,
                        'low': float(low) if low and low != '' else 0,
                        'open': float(open_price) if open_price and open_price != '' else 0,
                        'pre_close': float(pre_close) if pre_close and pre_close != '' else 0,
                    }
                except Exception:
                    continue

            time.sleep(0.1)  # 批次间隔，避免触发限流

        except Exception as e:
            print(f"  [WARN] 腾讯行情 batch {i//batch_size+1} 获取失败: {e}")
            continue

    return result


def fetch_volume_and_hot_tencent() -> tuple:
    """
    【首选方案】东方财富 datacenter 股票列表 + 腾讯批量行情
    返回: (volume_df, hot_df)
      - volume_df: 按成交额排序 TOP N
      - hot_df: 按 换手率×|涨跌幅| 排序 TOP N（热度代理指标）
    """
    print("[0/3] 正在通过 东方财富datacenter + 腾讯行情 获取全市场数据...")

    # Step A: 获取全部A股代码
    codes = _get_all_a_stock_codes()
    if not codes:
        print("  [WARN] 无法获取A股代码列表")
        return pd.DataFrame(), pd.DataFrame()

    # Step B: 批量获取腾讯行情
    print(f"  [INFO] 正在通过腾讯接口获取 {len(codes)} 只股票行情（分批）...")
    quotes = _fetch_tencent_batch_quotes(codes)
    if not quotes:
        print("  [WARN] 腾讯行情获取失败（全部批次为空）")
        return pd.DataFrame(), pd.DataFrame()

    print(f"  [OK] 成功获取 {len(quotes)} 只股票行情")

    # Step C: 构建 DataFrame
    records = []
    for code, q in quotes.items():
        records.append({
            'code': code,
            'name': q.get('name', ''),
            'close': q.get('close', 0),
            'pct_change': q.get('pct_change', 0),
            'volume_amount': q.get('volume_amount', 0),  # 单位: 万元
            'turnover_rate': q.get('turnover_rate', 0),
            'high': q.get('high', 0),
            'low': q.get('low', 0),
            'open': q.get('open', 0),
            'pre_close': q.get('pre_close', 0),
        })

    df = pd.DataFrame(records)

    # 过滤掉无效数据（价格为0或成交额为0的）
    df = df[(df['close'] > 0) & (df['volume_amount'] > 0)]

    if df.empty:
        print("  [WARN] 腾讯行情返回数据均为无效")
        return pd.DataFrame(), pd.DataFrame()

    # 腾讯返回的成交额单位是"万元"，转换为"元"以与其他数据源统一
    df['volume_amount'] = df['volume_amount'] * 10000

    # Step D: 成交额排名 TOP N
    df = df.sort_values('volume_amount', ascending=False).head(max(VOLUME_TOP_N, HOT_TOP_N) * 3)
    df['rank'] = range(1, len(df) + 1)

    volume_df = df.head(VOLUME_TOP_N).copy()
    volume_df['rank'] = range(1, len(volume_df) + 1)
    volume_df = volume_df.reset_index(drop=True)

    # Step E: 热度排名（换手率 × |涨跌幅| 作为热度代理）
    df['hot_value'] = df['turnover_rate'] * df['pct_change'].abs()
    hot_df = df.sort_values('hot_value', ascending=False).head(HOT_TOP_N).copy()
    hot_df['hot_rank'] = range(1, len(hot_df) + 1)
    hot_df = hot_df.reset_index(drop=True)

    print(f"  [OK] 腾讯行情方案: 成交额TOP{len(volume_df)}只, 热度TOP{len(hot_df)}只")
    return volume_df, hot_df


def _retry_call(fn, max_retries=RETRY_MAX, delay=RETRY_DELAY):
    """带重试的函数调用"""
    last_exc = None
    for i in range(max_retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < max_retries - 1:
                print(f"  [RETRY] 第{i+1}次失败，{delay}秒后重试...")
                time.sleep(delay)
    raise last_exc


def _extract_code(code_raw) -> str:
    """统一提取6位数字代码"""
    s = str(code_raw).strip()
    s = s.replace('sh', '').replace('sz', '').replace('bj', '').replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    m = re.search(r'(\d{6})', s)
    return m.group(1) if m else s


# ==================== 数据源0: dcsdk（首选，用于大盘/板块/K线/涨跌停） ====================

def fetch_market_summary() -> dict:
    """获取大盘行情概览"""
    if not DCSDK_AVAILABLE:
        return {}
    try:
        return market_summary()
    except Exception as e:
        print(f"  [WARN] dcsdk 大盘行情获取失败: {e}")
        return {}


def fetch_board_industry() -> list:
    """获取行业板块排行"""
    if not DCSDK_AVAILABLE:
        return []
    try:
        return board_industry()
    except Exception as e:
        print(f"  [WARN] dcsdk 行业板块获取失败: {e}")
        return []


def fetch_board_concept() -> list:
    """获取概念板块排行"""
    if not DCSDK_AVAILABLE:
        return []
    try:
        return board_concept()
    except Exception as e:
        print(f"  [WARN] dcsdk 概念板块获取失败: {e}")
        return []


def fetch_limit_scan() -> dict:
    """获取涨跌停监控"""
    if not DCSDK_AVAILABLE:
        return {}
    try:
        return limit_scan()
    except Exception as e:
        print(f"  [WARN] dcsdk 涨跌停监控获取失败: {e}")
        return {}


def fetch_stock_quote_dcsdk(code: str) -> dict:
    """通过 dcsdk 获取单只股票实时行情"""
    if not DCSDK_AVAILABLE:
        return {}
    try:
        return quote(str(code))
    except Exception as e:
        print(f"  [WARN] dcsdk 行情 {code} 获取失败: {e}")
        return {}


def fetch_stock_quotes_batch(codes: list) -> dict:
    """通过 dcsdk 批量获取实时行情，返回 {code: dict}"""
    if not DCSDK_AVAILABLE or not codes:
        return {}
    try:
        return quote_batch([str(c) for c in codes])
    except Exception as e:
        print(f"  [WARN] dcsdk 批量行情获取失败: {e}")
        return {}


def fetch_kline_dcsdk(code: str, freq: str = "day") -> list:
    """通过 dcsdk 获取K线数据"""
    if not DCSDK_AVAILABLE:
        return []
    try:
        return kline(str(code), freq)
    except Exception as e:
        print(f"  [WARN] dcsdk K线 {code} 获取失败: {e}")
        return []


# ==================== 数据源1: pywencai（主） ====================

def fetch_volume_top_wencai(date_str: str) -> pd.DataFrame:
    """通过 pywencai 获取成交额排名前N的股票"""
    print(f"[1/3] 正在通过 pywencai 获取{date_str}成交额前{VOLUME_TOP_N}...")
    query = f'{date_str}成交额前{VOLUME_TOP_N}'
    try:
        df = pywencai.get(query=query, loop=True)
        if df is None or df.empty:
            print(f"  [WARN] pywencai 返回空数据（query={query}）")
            return pd.DataFrame()
    except Exception as e:
        print(f"  [WARN] pywencai 查询异常: {e}")
        return pd.DataFrame()
    time.sleep(1)
    return df


def fetch_hot_top_wencai(date_str: str) -> pd.DataFrame:
    """通过 pywencai 获取热度排名前N的股票"""
    print(f"[2/3] 正在通过 pywencai 获取{date_str}个股热度前{HOT_TOP_N}...")
    query = f'{date_str}个股热度前{HOT_TOP_N}'
    try:
        df = pywencai.get(query=query, loop=True)
        if df is None or df.empty:
            print(f"  [WARN] pywencai 返回空数据（query={query}）")
            return pd.DataFrame()
    except Exception as e:
        print(f"  [WARN] pywencai 查询异常: {e}")
        return pd.DataFrame()
    time.sleep(1)
    return df


# ==================== 数据源2: 同花顺热点接口（辅） ====================

def fetch_hotspot_10jqka() -> pd.DataFrame:
    """
    同花顺热点接口 — 当日强势股 + 人工标注题材归因
    注意：需在15:30后调用才稳定（收盘后更新）
    """
    url = "https://eq.10jqka.com.cn/open/api/hot_list/v1/rank"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.10jqka.com.cn/",
    }
    params = {"type": "stock", "size": HOT_TOP_N}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("status_code") == 0 and "data" in data:
            items = data["data"].get("list", [])
            records = []
            for item in items:
                code = str(item.get("code", "")).strip()
                records.append({
                    "code": _extract_code(code),
                    "name": item.get("name", ""),
                    "hot_rank": item.get("ranking", 0),
                    "reason_tags": item.get("reason", ""),
                    "pct_change": item.get("change", 0),
                })
            df = pd.DataFrame(records)
            if not df.empty:
                df["hot_value"] = df["hot_rank"].apply(lambda x: max(1, len(df) - x + 1))
                df["source"] = "10jqka"
            return df
    except Exception as e:
        print(f"  [INFO] 同花顺热点接口不可用: {e}")
    return pd.DataFrame()


# ==================== 数据源3: akshare（兜底） ====================

def fetch_volume_top_akshare(date_str: str) -> pd.DataFrame:
    """
    通过 akshare 获取当日成交额排名前N的股票
    注意：此接口底层打东方财富，有反爬风险，仅作兜底
    """
    print(f"[1/3] 正在通过 akshare 获取当日成交额前{VOLUME_TOP_N}（仅兜底）...")
    try:
        df = ak.stock_zh_a_spot_em()
        time.sleep(1)
    except (requests.ConnectionError, requests.Timeout, ConnectionError) as e:
        print(f"  [WARN] akshare 网络连接失败（可能是反爬或网络问题）: {e}")
        return pd.DataFrame()
    except Exception as e:
        print(f"  [WARN] akshare 实时行情获取失败: {e}")
        return pd.DataFrame()

    col_map = {}
    for col in df.columns:
        c = str(col)
        if '代码' in c and '名称' not in c:
            col_map[col] = 'code'
        elif '名称' in c and '代码' not in c:
            col_map[col] = 'name'
        elif '成交额' in c or '金额' in c:
            col_map[col] = 'volume_amount'
        elif '涨跌幅' in c or '涨幅' in c:
            col_map[col] = 'pct_change'
        elif '最新价' in c or '现价' in c or '收盘价' in c:
            col_map[col] = 'close'
        elif '成交量' in c or '成交' in c:
            col_map[col] = 'volume_shares'
        elif '换手率' in c:
            col_map[col] = 'turnover_rate'
    df = df.rename(columns=col_map)

    if 'volume_amount' in df.columns:
        df['volume_amount'] = pd.to_numeric(df['volume_amount'], errors='coerce')
        df = df.sort_values('volume_amount', ascending=False).head(VOLUME_TOP_N).reset_index(drop=True)
        df['rank'] = range(1, len(df) + 1)
    else:
        for col in df.columns:
            if any(k in str(col) for k in ['金额', '成交', 'volume', 'turnover']):
                df['volume_amount'] = df[col]
                break

    if 'code' in df.columns:
        df['code'] = df['code'].apply(_extract_code)
    return df


def fetch_hot_top_akshare(date_str: str) -> pd.DataFrame:
    """
    通过 akshare 获取当日热度排名前N的股票
    使用换手率 + 涨跌幅作为"热度"代理指标
    注意：此接口底层打东方财富，有反爬风险，仅作兜底
    """
    print(f"[2/3] 正在通过 akshare 获取当日热度前{HOT_TOP_N}（仅兜底）...")

    def _call():
        df = ak.stock_zh_a_spot_em()
        time.sleep(1)
        return df

    try:
        df = _retry_call(_call)
    except (requests.ConnectionError, requests.Timeout, ConnectionError) as e:
        print(f"  [WARN] akshare 网络连接失败（已重试{RETRY_MAX}次，可能是反爬或网络问题）: {e}")
        return pd.DataFrame()
    except Exception as e:
        print(f"  [WARN] akshare 实时行情获取失败（已重试{RETRY_MAX}次）: {e}")
        return pd.DataFrame()

    col_map = {}
    for col in df.columns:
        c = str(col)
        if '代码' in c and '名称' not in c:
            col_map[col] = 'code'
        elif '名称' in c and '代码' not in c:
            col_map[col] = 'name'
        elif '涨跌幅' in c or '涨幅' in c:
            col_map[col] = 'pct_change'
        elif '换手率' in c:
            col_map[col] = 'turnover_rate'
        elif '成交额' in c or '金额' in c:
            col_map[col] = 'volume_amount'
        elif '最新价' in c or '现价' in c:
            col_map[col] = 'close'
    df = df.rename(columns=col_map)

    df['turnover_rate'] = pd.to_numeric(df.get('turnover_rate', 0), errors='coerce').fillna(0)
    df['pct_change'] = pd.to_numeric(df.get('pct_change', 0), errors='coerce').fillna(0)
    df['hot_value'] = df['turnover_rate'] * df['pct_change'].abs()
    df = df.sort_values('hot_value', ascending=False).head(HOT_TOP_N).reset_index(drop=True)
    df['hot_rank'] = range(1, len(df) + 1)

    if 'code' in df.columns:
        df['code'] = df['code'].apply(_extract_code)
    return df


# ==================== 列名标准化 ====================

def standardize_columns(df: pd.DataFrame, data_type: str) -> pd.DataFrame:
    """统一列名，方便后续处理"""
    col_map = {}
    for col in df.columns:
        if col in ('code', 'market_code'):
            continue
        if '股票代码' in col:
            continue
        elif '简称' in col:
            col_map[col] = 'name'
        elif '成交额排名' in col:
            col_map[col] = 'rank'
        elif '成交额[' in col:
            col_map[col] = 'volume_amount'
        elif '个股热度排名' in col:
            col_map[col] = 'hot_rank'
        elif '个股热度[' in col:
            col_map[col] = 'hot_value'
        elif '涨跌幅' in col:
            col_map[col] = 'pct_change'
        elif '最新价' in col:
            col_map[col] = 'close'
        elif '成交量[' in col:
            col_map[col] = 'volume_shares'
        elif '换手率' in col:
            col_map[col] = 'turnover_rate'
        elif 'reason_tags' in col:
            col_map[col] = 'reason_tags'

    df = df.rename(columns=col_map)
    df['data_type'] = data_type
    return df


# ==================== 主入口：多级回退机制 ====================

def fetch_data(date_str: str) -> tuple:
    """
    获取数据，按优先级依次尝试：
      1. 东方财富 datacenter + 腾讯 qt.gtimg.cn（批量行情，成交额+热度排名）
      2. pywencai（自然语言选股）
      3. 同花顺热点接口（作为热度数据补充）
      4. akshare（仅作兜底，注意反爬限制）

    返回: (volume_df, hot_df, source)
    """
    volume_df = None
    hot_df = None
    source = 'tencent'

    # ---- 第一优先：datacenter + 腾讯行情（主力方案） ----
    try:
        volume_df, hot_df = fetch_volume_and_hot_tencent()
        if volume_df is not None and not volume_df.empty and hot_df is not None and not hot_df.empty:
            print("  [OK] 腾讯行情方案数据获取成功")
            source = 'tencent'

            # 尝试补充同花顺热点标签（非阻塞）
            hot_10jqka = fetch_hotspot_10jqka()
            if not hot_10jqka.empty:
                source = 'tencent + 10jqka热点'
                print(f"  [OK] 同花顺热点标签补充成功（{len(hot_10jqka)}只）")

            volume_df = standardize_columns(volume_df, 'volume')
            hot_df = standardize_columns(hot_df, 'hot')
            _finalize_codes(volume_df, hot_df)
            return volume_df, hot_df, source
    except Exception as e:
        print(f"  [WARN] 腾讯行情方案获取失败: {e}")

    # ---- 第二优先：pywencai ----
    if PYWENCAI_AVAILABLE:
        try:
            volume_df = fetch_volume_top_wencai(date_str)
            hot_df = fetch_hot_top_wencai(date_str)
            if volume_df is not None and not volume_df.empty and hot_df is not None and not hot_df.empty:
                source = 'pywencai'
                print("  [OK] pywencai 数据获取成功")

                hot_10jqka = fetch_hotspot_10jqka()
                if not hot_10jqka.empty:
                    source = 'pywencai + 10jqka热点'
                    print(f"  [OK] 同花顺热点标签补充成功（{len(hot_10jqka)}只）")

                volume_df = standardize_columns(volume_df, 'volume')
                hot_df = standardize_columns(hot_df, 'hot')
                _finalize_codes(volume_df, hot_df)
                return volume_df, hot_df, source
        except Exception as e:
            print(f"  [WARN] pywencai 获取失败: {e}")

    # ---- 第三优先：akshare（兜底） ----
    print("  [INFO] 回退到 akshare（注意：行情类接口有反爬风险）...")
    source = 'akshare'

    if not AKSHARE_AVAILABLE:
        raise RuntimeError(
            "所有数据源均不可用（tencent/pywencai/akshare 均失败）。\n"
            "可能原因：\n"
            "  1. 网络连接不稳定\n"
            "  2. 所有数据源接口均被限制\n"
            "  3. 非交易时段（建议在 9:30-15:30 期间运行）\n"
            "建议：等待几分钟后重试，或检查网络代理设置"
        )

    volume_df = fetch_volume_top_akshare(date_str)
    hot_df = fetch_hot_top_akshare(date_str)

    if volume_df.empty or hot_df.empty:
        error_detail = []
        if volume_df.empty:
            error_detail.append("成交额数据为空")
        if hot_df.empty:
            error_detail.append("热度数据为空")
        detail = "；".join(error_detail)
        raise RuntimeError(
            f"所有数据源均返回空数据（{detail}）。\n"
            "可能原因：\n"
            "  1. 东方财富/问财接口反爬限制，当前IP被暂时屏蔽\n"
            "  2. 网络连接不稳定，无法访问数据源\n"
            "  3. 非交易时段（建议在 9:30-15:30 期间运行）\n"
            "建议：等待几分钟后重试，或检查网络代理设置"
        )

    print("  [OK] akshare 兜底数据获取成功")

    volume_df = standardize_columns(volume_df, 'volume')
    hot_df = standardize_columns(hot_df, 'hot')
    _finalize_codes(volume_df, hot_df)
    return volume_df, hot_df, source


def _finalize_codes(volume_df, hot_df):
    """统一 code 列为6位字符串"""
    if 'code' in volume_df.columns:
        volume_df['code'] = volume_df['code'].astype(str).str.extract(r'(\d{6})')[0]
    if 'code' in hot_df.columns:
        hot_df['code'] = hot_df['code'].astype(str).str.extract(r'(\d{6})')[0]


def main():
    today = datetime.now().strftime('%Y%m%d')
    output_dir = os.path.join(OUTPUT_DIR, today)
    os.makedirs(output_dir, exist_ok=True)

    print(f"{'='*50}")
    print(f"  短线助手 — 数据获取")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  数据链: 腾讯行情(主力) → pywencai(备) → 同花顺热点 → akshare(兜底)")
    print(f"{'='*50}")

    volume_df, hot_df, source = fetch_data(today)

    print(f"\n  数据源: {source}")
    print(f"  成交额排名: {len(volume_df)} 条")
    print(f"  热度排名: {len(hot_df)} 条")

    # ---- 保存 ----
    output_path = os.path.join(output_dir, 'raw_data.xlsx')
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        volume_df.to_excel(writer, sheet_name='成交额排名', index=False)
        hot_df.to_excel(writer, sheet_name='热度排名', index=False)

    print(f"\n[OK] 数据已保存: {output_path}")

    print(f"\n{'—'*40}")
    print("成交额前5预览:")
    cols_show = [c for c in ['code', 'name', 'volume_amount', 'pct_change'] if c in volume_df.columns]
    if cols_show:
        print(volume_df[cols_show].head(5).to_string(index=False))

    print(f"\n热度前5预览:")
    cols_show = [c for c in ['code', 'name', 'hot_value', 'pct_change'] if c in hot_df.columns]
    if cols_show:
        print(hot_df[cols_show].head(5).to_string(index=False))


if __name__ == '__main__':
    main()

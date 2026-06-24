"""
短线助手 — 筛选与动量评分模块
1. 取成交额TOP + 热度TOP的交集
2. Min-Max标准化后计算综合评分，取前N
3. 25日线性回归动量评分，最终输出TOP股票

K线数据策略：
  首选: dcsdk.kline（实时行情、K线、大盘、板块）
  备:   mootdx → 通达信TCP协议，零鉴权不封IP
  兜底: akshare → 仅兜底（底层打东财有反爬风险）

存储: SQLite 数据库（data/shortais.db）
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import time
import re
import warnings
warnings.filterwarnings('ignore')

from sklearn.linear_model import LinearRegression

try:
    from dcsdk import kline as dcsdk_kline
    DCSDK_AVAILABLE = True
except Exception:
    DCSDK_AVAILABLE = False

try:
    from mootdx.quotes import Quotes
    MOOTDX_AVAILABLE = True
except Exception:
    MOOTDX_AVAILABLE = False

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except Exception:
    AKSHARE_AVAILABLE = False

from config import (
    VOLUME_TOP_N, HOT_TOP_N, FINAL_TOP_N,
    MOMENTUM_DAYS, MOMENTUM_TOP_N, OUTPUT_DIR,
    SCORING_VERSION,
    WEIGHT_VOLUME, WEIGHT_HOT, WEIGHT_MOMENTUM,
    WEIGHT_PERIOD_STRENGTH, WEIGHT_TREND_QUALITY, WEIGHT_LIQUIDITY, WEIGHT_SECTOR,
    PENALTY_DRAWDOWN, PENALTY_OVERHEAT, PENALTY_VOLATILITY,
    RECENT_SPIKE_3D_LIMIT, RECENT_SPIKE_5D_LIMIT, DAILY_SPIKE_LIMIT,
    MAX_DRAWDOWN_LIMIT, VOLATILITY_LIMIT,
)
from database import load_raw_volume, load_raw_hot
from sector import (
    compute_sector_strength, build_stock_sector_map, neutral_sector_payload,
    infer_industry_by_name, build_cls_stock_sector_map
)


def load_raw_data(date_str: str) -> tuple:
    """从数据库加载原始数据"""
    volume_df = load_raw_volume(date_str)
    hot_df = load_raw_hot(date_str)
    if volume_df.empty or hot_df.empty:
        raise FileNotFoundError(f"日期 {date_str} 的原始数据未入库，请先运行数据获取")
    return volume_df, hot_df


def min_max_normalize(series: pd.Series) -> pd.Series:
    """Min-Max标准化到[0, 1]区间"""
    s = pd.to_numeric(series, errors='coerce')
    if s.max() == s.min():
        return pd.Series([0.5] * len(s), index=s.index)
    return (s - s.min()) / (s.max() - s.min())


def percentile_score(series: pd.Series, higher_better: bool = True) -> pd.Series:
    """稳健排名分位数，返回[0, 1]；比Min-Max更不容易被极端值扭曲。"""
    s = pd.to_numeric(series, errors='coerce')
    if len(s) <= 1 or s.nunique(dropna=True) <= 1:
        return pd.Series([0.5] * len(s), index=s.index)
    ranks = s.rank(method='average', ascending=not higher_better)
    return 1 - (ranks - 1) / (len(s) - 1)


def _clip01(value: float) -> float:
    """限制到[0, 1]区间。"""
    if pd.isna(value):
        return 0.0
    return float(np.clip(value, 0.0, 1.0))


def rank_order_score(length: int) -> pd.Series:
    """按榜单顺序生成[1, 0]分位分，第一名最高。"""
    if length <= 1:
        return pd.Series([1.0] * length)
    return pd.Series(1 - np.arange(length) / (length - 1))


def normalize_stock_code(value) -> str | None:
    """把各种来源的股票代码统一成6位数字，兼容 1、1.0、000001.SZ 等格式。"""
    if pd.isna(value):
        return None
    s = str(value).strip()
    if s.endswith('.0'):
        s = s[:-2]
    m = re.search(r'(\d{6})', s)
    if m:
        return m.group(1)
    digits = ''.join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    if len(digits) <= 6:
        return digits.zfill(6)
    return digits[:6]


def compute_composite_score(volume_df: pd.DataFrame, hot_df: pd.DataFrame) -> pd.DataFrame:
    """
    核心筛选逻辑:
    1. 取两个股池的交集
    2. 对成交额和热度分别做排名分位数，降低极端值影响
    3. 综合评分 = 资金分位与热度分位加权
    """
    volume_df = volume_df.copy()
    hot_df = hot_df.copy()
    volume_df['code'] = volume_df['code'].map(normalize_stock_code)
    hot_df['code'] = hot_df['code'].map(normalize_stock_code)

    volume_codes = set(volume_df['code'].dropna())
    hot_codes = set(hot_df['code'].dropna())

    common_codes = volume_codes.intersection(hot_codes)
    print(f"  成交额榜股票数: {len(volume_codes)}")
    print(f"  热度榜股票数:   {len(hot_codes)}")
    print(f"  交集股票数:     {len(common_codes)}")

    vol_cols = [c for c in ['code', 'name', 'volume_amount', 'rank', 'pct_change', 'turnover_rate'] if c in volume_df.columns]
    hot_cols = [c for c in ['code', 'name', 'hot_value', 'hot_rank', 'reason_tags'] if c in hot_df.columns]
    vol_sub = volume_df[vol_cols].copy()
    hot_sub = hot_df[hot_cols].copy()
    vol_sub['has_volume'] = True
    hot_sub['has_hot'] = True
    vol_sub['_volume_rank_score'] = rank_order_score(len(vol_sub)).values
    hot_sub['_hot_rank_score'] = rank_order_score(len(hot_sub)).values

    merge_how = 'inner' if len(common_codes) >= FINAL_TOP_N else 'outer'
    if merge_how == 'outer':
        if len(common_codes) == 0:
            print("  [WARN] 交集为空，启用软合并模式：缺失的资金/热度维度按0分处理")
        else:
            print(f"  [WARN] 交集仅 {len(common_codes)} 只，不足 {FINAL_TOP_N}，启用软合并补位")

    merged = vol_sub.merge(hot_sub, on='code', how=merge_how, suffixes=('_volume', '_hot'))
    merged.drop_duplicates(subset=['code'], inplace=True)
    if 'name_volume' in merged.columns:
        merged['name'] = merged['name_volume']
    if 'name_hot' in merged.columns:
        merged['name'] = merged.get('name', pd.Series(index=merged.index, dtype=object)).fillna(merged['name_hot'])
    if 'name' not in merged.columns:
        merged['name'] = ''

    merged['has_volume'] = merged.get('has_volume', False).fillna(False).astype(bool)
    merged['has_hot'] = merged.get('has_hot', False).fillna(False).astype(bool)
    merged['candidate_mode'] = np.where(merged['has_volume'] & merged['has_hot'], 'intersection', 'soft_union')

    volume_base = np.log1p(pd.to_numeric(merged['volume_amount'], errors='coerce').fillna(0))
    if volume_base[merged['has_volume']].nunique(dropna=True) > 1:
        volume_score = percentile_score(volume_base)
    else:
        volume_score = pd.to_numeric(merged.get('_volume_rank_score', 0), errors='coerce').fillna(0)
    hot_base = pd.to_numeric(merged['hot_value'], errors='coerce').fillna(0)
    if hot_base[merged['has_hot']].nunique(dropna=True) > 1:
        hot_score = percentile_score(hot_base)
    else:
        hot_score = pd.to_numeric(merged.get('_hot_rank_score', 0), errors='coerce').fillna(0)
    merged['volume_score'] = volume_score.where(merged['has_volume'], 0.0)
    merged['hot_score'] = hot_score.where(merged['has_hot'], 0.0)
    # 兼容旧字段名，前端和历史报告仍可读取。
    merged['vol_norm'] = merged['volume_score']
    merged['hot_norm'] = merged['hot_score']
    merged['composite_score'] = (
        WEIGHT_VOLUME * merged['volume_score'] +
        WEIGHT_HOT * merged['hot_score']
    ) / max(WEIGHT_VOLUME + WEIGHT_HOT, 1e-9)
    merged.sort_values('composite_score', ascending=False, inplace=True)
    merged['composite_rank'] = range(1, len(merged) + 1)

    keep_cols = [
        'code', 'name', 'volume_amount', 'hot_value', 'vol_norm', 'hot_norm',
        'volume_score', 'hot_score', 'composite_score', 'composite_rank',
        'has_volume', 'has_hot', 'candidate_mode'
    ]
    return merged[[c for c in keep_cols if c in merged.columns]].head(FINAL_TOP_N).reset_index(drop=True)


def attach_sector_strength(candidates: pd.DataFrame, date_str: str, force_refresh: bool = False) -> pd.DataFrame:
    """为候选股附加行业板块强度。"""
    if candidates.empty:
        return candidates

    try:
        cls_map = build_cls_stock_sector_map(candidates, date_str, force_refresh=force_refresh)
        sector_map = dict(cls_map)
        missing_count = len(candidates) - len(sector_map)
        if missing_count > 0:
            sector_df = compute_sector_strength(date_str, force_refresh=force_refresh)
            ak_map = build_stock_sector_map(sector_df)
            for code, payload in ak_map.items():
                sector_map.setdefault(code, payload)
        else:
            sector_df = pd.DataFrame()
        if sector_df.empty:
            print("  [WARN] 行业板块强度表为空，将优先使用财联社热门板块/本地规则补板块")
    except Exception as e:
        print(f"  [WARN] 板块强度获取失败: {e}")
        sector_map = build_cls_stock_sector_map(candidates, date_str, force_refresh=force_refresh)

    enriched = candidates.copy()
    sector_payloads = []
    for _, row in enriched.iterrows():
        code = str(row.get('code', ''))
        payload = sector_map.get(str(code), neutral_sector_payload())
        if payload.get('sector_name') == '未知':
            industry = infer_industry_by_name(str(row.get('name', '')))
            if industry:
                payload = {**payload, 'sector_name': industry, 'sector_source': 'local'}
        sector_payloads.append(payload)

    sector_df2 = pd.DataFrame(sector_payloads)
    enriched = pd.concat([enriched.reset_index(drop=True), sector_df2.reset_index(drop=True)], axis=1)
    enriched['sector_strength'] = pd.to_numeric(enriched.get('sector_strength', 0), errors='coerce').fillna(0.5)
    if enriched['sector_strength'].nunique(dropna=True) <= 1 and 'sector_name' in enriched.columns:
        known = enriched['sector_name'].astype(str).ne('未知')
        counts = enriched.loc[known, 'sector_name'].value_counts()
        if not counts.empty:
            max_count = counts.max()
            count_score = enriched['sector_name'].map(counts).fillna(0) / max(max_count, 1)
            # 候选池兜底强度：同板块入选越多越强，保持在0.45~0.75，避免替代真实板块数据。
            enriched['sector_strength'] = np.where(known, 0.45 + 0.30 * count_score, 0.5)
    enriched['sector_change'] = pd.to_numeric(enriched.get('sector_change', 0), errors='coerce').fillna(0)
    enriched['sector_rise_ratio'] = pd.to_numeric(enriched.get('sector_rise_ratio', 0), errors='coerce').fillna(0)
    enriched['sector_limit_up_count'] = pd.to_numeric(enriched.get('sector_limit_up_count', 0), errors='coerce').fillna(0)
    return enriched


# ==================== K线数据获取（mootdx 主，akshare 备） ====================

# mootdx 连接池（单例，自动重连）
_mootdx_client = None
_mootdx_init_ok = False
_MOOTDX_MAX_RETRIES = 3
_AKSHARE_MAX_RETRIES = 2
_RETRY_BACKOFF = 1.5   # 重试间隔倍数


def _get_mootdx_client():
    """获取 mootdx 行情客户端（带重连机制 + 超时保护）"""
    global _mootdx_client, _mootdx_init_ok
    if _mootdx_client is not None:
        return _mootdx_client
    if _mootdx_init_ok is False:
        for attempt in range(_MOOTDX_MAX_RETRIES):
            try:
                _mootdx_client = Quotes.factory(market='std', timeout=8)
                # 快速连接验证：取一只股票1条日线（带超时保护）
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_mootdx_client.bars, symbol='600519', frequency=4, offset=1)
                    try:
                        test = future.result(timeout=12)
                    except concurrent.futures.TimeoutError:
                        print(f"  [WARN] mootdx 连接验证超时（尝试{attempt+1}/{_MOOTDX_MAX_RETRIES}）")
                        _mootdx_client = None
                        if attempt < _MOOTDX_MAX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF ** attempt)
                        continue
                if test is not None and not test.empty:
                    _mootdx_init_ok = True
                    print("  [OK] mootdx 通达信TCP连接成功（零鉴权，不封IP）")
                    return _mootdx_client
            except Exception as e:
                print(f"  [WARN] mootdx 连接尝试{attempt+1}/{_MOOTDX_MAX_RETRIES}失败: {e}")
                _mootdx_client = None
                if attempt < _MOOTDX_MAX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF ** attempt)
        _mootdx_init_ok = False
        print("  [WARN] mootdx 连接全部失败，将使用 akshare")
    return _mootdx_client


def _mootdx_to_symbol(code: str) -> str:
    """6位代码 → mootdx 格式（market='std' 只需6位数字，不要加sh/sz前缀）"""
    return str(code).zfill(6)


def _normalize_mootdx_df(df, keep_col='收盘') -> pd.DataFrame:
    """将 mootdx 返回的 dataframe 列名标准化为中文"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.reset_index(drop=True)
    col_map = {
        'open': '开盘', 'close': '收盘', 'high': '最高',
        'low': '最低', 'vol': '成交量', 'amount': '成交额',
        'datetime': '日期', 'volume': '成交量',
    }
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)
    if '日期' in df.columns:
        df['日期'] = pd.to_datetime(df['日期']).dt.strftime('%Y-%m-%d')
    return df


def fetch_kline_mootdx(code: str, count: int) -> pd.DataFrame:
    """
    通过 mootdx 通达信TCP协议获取日K线（3次重试 + 双frequency尝试 + 超时保护）
    """
    client = _get_mootdx_client()
    if client is None:
        return pd.DataFrame()

    symbol = _mootdx_to_symbol(code)
    freq_order = [9, 4]  # 先试9再试4

    for attempt in range(_MOOTDX_MAX_RETRIES):
        for freq in freq_order:
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(client.bars, symbol=symbol, frequency=freq, offset=count)
                    try:
                        df = future.result(timeout=15)
                    except concurrent.futures.TimeoutError:
                        print(f"    [WARN] mootdx {code} 请求超时 (freq={freq})")
                        continue
                if df is not None and not df.empty:
                    return _normalize_mootdx_df(df)
            except Exception:
                pass
        if attempt < _MOOTDX_MAX_RETRIES - 1:
            time.sleep(_RETRY_BACKOFF ** attempt)
            # 重建客户端
            global _mootdx_client
            _mootdx_client = None
            client = _get_mootdx_client()
            if client is None:
                break

    return pd.DataFrame()


def fetch_kline_akshare(code: str, days: int, end_date: str) -> pd.DataFrame:
    """
    akshare 获取个股历史日线（仅兜底，2次重试）
    """
    end_dt = datetime.strptime(end_date, '%Y%m%d')
    start_dt = end_dt - timedelta(days=days * 3)

    for attempt in range(_AKSHARE_MAX_RETRIES):
        try:
            df = ak.stock_zh_a_hist(
                symbol=str(code),
                period="daily",
                start_date=start_dt.strftime('%Y%m%d'),
                end_date=end_dt.strftime('%Y%m%d'),
                adjust="qfq"
            )
            if df is not None and not df.empty:
                return df.tail(days)
        except Exception as e:
            if attempt < _AKSHARE_MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))  # 逐次增加等待
                continue
            print(f"    [WARN] akshare {code} 历史数据获取失败: {e}")

    return pd.DataFrame()


def fetch_stock_history_dcsdk(code: str, days: int) -> pd.DataFrame:
    """
    通过 dcsdk 获取个股历史日线数据
    返回标准化 DataFrame（含 日期/开盘/收盘/最高/最低/成交量 列）
    """
    if not DCSDK_AVAILABLE:
        return pd.DataFrame()
    try:
        raw = dcsdk_kline(str(code), "day")
        if not raw:
            return pd.DataFrame()
        df = pd.DataFrame(raw)
        # dcsdk kline 返回: date/open/close/high/low/volume
        col_map = {
            'date': '日期', 'open': '开盘', 'close': '收盘',
            'high': '最高', 'low': '最低', 'volume': '成交量',
        }
        df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)
        if '日期' in df.columns:
            df['日期'] = pd.to_datetime(df['日期']).dt.strftime('%Y-%m-%d')
        return df.tail(days)
    except Exception as e:
        print(f"    [WARN] dcsdk K线 {code} 获取失败: {e}")
        return pd.DataFrame()


def fetch_stock_history(code: str, days: int, end_date: str) -> pd.DataFrame:
    """
    获取个股历史日线数据
    路径: dcsdk → mootdx(try 3次) → akshare(try 2次) → 空
    每只股票总共有 6 次机会，极大降低"无数据"概率
    """
    # 路径1: dcsdk（首选）
    if DCSDK_AVAILABLE:
        df = fetch_stock_history_dcsdk(code, days)
        if not df.empty:
            return df

    # 路径2: mootdx
    if MOOTDX_AVAILABLE:
        df = fetch_kline_mootdx(code, days)
        if not df.empty:
            return df

    # 路径3: akshare 兜底
    if AKSHARE_AVAILABLE:
        df = fetch_kline_akshare(code, days, end_date)
        if not df.empty:
            return df

    print(f"    [FAIL] {code} 所有数据源均失败（dcsdk + mootdx + akshare）")
    return pd.DataFrame()


# ==================== 动量评分 ====================

def compute_momentum_score(prices: pd.Series) -> tuple:
    """
    线性回归动量评分
    对相对价格做线性回归，动量 = 10000 * 斜率 * R²
    斜率: 趋势强度  R²: 趋势可靠性
    """
    prices = prices.dropna().values
    if len(prices) < 5:
        return 0.0, 0.0, 0.0

    relative_prices = prices / prices[0]
    x = np.arange(len(relative_prices)).reshape(-1, 1)

    lr = LinearRegression()
    lr.fit(x, relative_prices)
    slope = lr.coef_[0]
    r_squared = lr.score(x, relative_prices)
    momentum = 10000 * slope * r_squared

    return momentum, slope, r_squared


def compute_momentum_features(kline_df: pd.DataFrame, price_col: str) -> dict:
    """计算动量、风险和过热特征。"""
    prices = pd.to_numeric(kline_df[price_col], errors='coerce').dropna()
    momentum, slope, r2 = compute_momentum_score(prices)
    if len(prices) < 2:
        return {
            'momentum_score': momentum,
            'trend_slope': slope,
            'trend_r2': r2,
            'risk_adj_momentum': 0.0,
            'period_change': 0.0,
            'recent_change_3d': 0.0,
            'recent_change_5d': 0.0,
            'latest_change': 0.0,
            'volatility': 0.0,
            'max_drawdown': 0.0,
            'overheat_penalty': 0.0,
            'drawdown_penalty': 0.0,
            'volatility_penalty': 0.0,
            'upper_shadow_penalty': 0.0,
        }

    returns = prices.pct_change().dropna()
    volatility = float(returns.std(ddof=0)) if len(returns) else 0.0
    period_change = float(prices.iloc[-1] / prices.iloc[0] - 1) if prices.iloc[0] else 0.0
    recent_change_3d = float(prices.iloc[-1] / prices.iloc[-4] - 1) if len(prices) >= 4 and prices.iloc[-4] else period_change
    recent_change_5d = float(prices.iloc[-1] / prices.iloc[-6] - 1) if len(prices) >= 6 and prices.iloc[-6] else period_change
    latest_change = float(returns.iloc[-1]) if len(returns) else 0.0

    running_max = prices.cummax()
    drawdowns = prices / running_max - 1
    max_drawdown = abs(float(drawdowns.min())) if len(drawdowns) else 0.0

    risk_adj_momentum = momentum / (1 + volatility * 10 + max_drawdown * 2)

    overheat_parts = [
        _clip01((recent_change_3d - RECENT_SPIKE_3D_LIMIT) / max(RECENT_SPIKE_3D_LIMIT, 1e-9)),
        _clip01((recent_change_5d - RECENT_SPIKE_5D_LIMIT) / max(RECENT_SPIKE_5D_LIMIT, 1e-9)),
        _clip01((latest_change - DAILY_SPIKE_LIMIT) / 0.05),
    ]

    upper_shadow_penalty = 0.0
    col_open = '开盘' if '开盘' in kline_df.columns else 'open'
    col_high = '最高' if '最高' in kline_df.columns else 'high'
    col_low = '最低' if '最低' in kline_df.columns else 'low'
    if all(c in kline_df.columns for c in [col_open, col_high, col_low, price_col]):
        last = kline_df.dropna(subset=[col_open, col_high, col_low, price_col]).tail(1)
        if not last.empty:
            open_p = float(last[col_open].iloc[0])
            high_p = float(last[col_high].iloc[0])
            low_p = float(last[col_low].iloc[0])
            close_p = float(last[price_col].iloc[0])
            price_range = max(high_p - low_p, 1e-9)
            upper_shadow = (high_p - max(open_p, close_p)) / price_range
            upper_shadow_penalty = _clip01((upper_shadow - 0.45) / 0.35)
            overheat_parts.append(upper_shadow_penalty)

    return {
        'momentum_score': momentum,
        'trend_slope': slope,
        'trend_r2': r2,
        'risk_adj_momentum': risk_adj_momentum,
        'period_change': period_change,
        'recent_change_3d': recent_change_3d,
        'recent_change_5d': recent_change_5d,
        'latest_change': latest_change,
        'volatility': volatility,
        'max_drawdown': max_drawdown,
        'overheat_penalty': float(np.mean(overheat_parts)) if overheat_parts else 0.0,
        'drawdown_penalty': _clip01(max_drawdown / max(MAX_DRAWDOWN_LIMIT, 1e-9)),
        'volatility_penalty': _clip01(volatility / max(VOLATILITY_LIMIT, 1e-9)),
        'upper_shadow_penalty': upper_shadow_penalty,
    }


def score_momentum_batch(candidates: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """
    对候选池中的所有股票做动量评分
    """
    kline_source = "dcsdk" if DCSDK_AVAILABLE else ("mootdx" if MOOTDX_AVAILABLE else ("akshare" if AKSHARE_AVAILABLE else "N/A"))
    print(f"\n[动量评分] 对{FINAL_TOP_N}只候选股做{MOMENTUM_DAYS}日动量分析 (K线源: {kline_source})...")
    results = []

    for i, row in candidates.iterrows():
        code = row['code']
        name = row.get('name', '')
        composite = row['composite_score']

        print(f"  [{i+1}/{len(candidates)}] {code} {name}...", end=' ')
        time.sleep(0.15)

        hist = fetch_stock_history(code, MOMENTUM_DAYS, date_str)
        if hist.empty:
            print("无数据")
            continue

        # 兼容 mootdx 返回的 '收盘' 和 akshare 返回的 '收盘'
        price_col = '收盘' if '收盘' in hist.columns else 'close'
        if price_col not in hist.columns:
            print("无价格列")
            continue

        features = compute_momentum_features(hist, price_col)
        momentum = features['momentum_score']
        slope = features['trend_slope']
        r2 = features['trend_r2']
        print(
            f"动量={momentum:.2f} 风险调整={features['risk_adj_momentum']:.2f} "
            f"(斜率={slope:.6f}, R²={r2:.4f})"
        )

        results.append({
            'code': code,
            'name': name,
            'volume_amount': row.get('volume_amount'),
            'hot_value': row.get('hot_value'),
            'vol_norm': row['vol_norm'],
            'hot_norm': row['hot_norm'],
            'volume_score': row.get('volume_score', row['vol_norm']),
            'hot_score': row.get('hot_score', row['hot_norm']),
            'candidate_mode': row.get('candidate_mode', 'intersection'),
            'has_volume': bool(row.get('has_volume', True)),
            'has_hot': bool(row.get('has_hot', True)),
            'sector_name': row.get('sector_name', '未知'),
            'sector_strength': round(float(row.get('sector_strength', 0.5) or 0.5), 4),
            'sector_change': round(float(row.get('sector_change', 0) or 0), 2),
            'sector_rise_ratio': round(float(row.get('sector_rise_ratio', 0) or 0), 4),
            'sector_limit_up_count': int(row.get('sector_limit_up_count', 0) or 0),
            'sector_source': row.get('sector_source', 'fallback'),
            'composite_score': round(composite, 4),
            'momentum_score': round(momentum, 2),
            'trend_slope': round(slope, 6),
            'trend_r2': round(r2, 4),
            'risk_adj_momentum': round(features['risk_adj_momentum'], 2),
            'period_change': round(features['period_change'] * 100, 2),
            'recent_change_3d': round(features['recent_change_3d'] * 100, 2),
            'recent_change_5d': round(features['recent_change_5d'] * 100, 2),
            'latest_change': round(features['latest_change'] * 100, 2),
            'volatility': round(features['volatility'] * 100, 2),
            'max_drawdown': round(features['max_drawdown'] * 100, 2),
            'overheat_penalty': round(features['overheat_penalty'], 4),
            'drawdown_penalty': round(features['drawdown_penalty'], 4),
            'volatility_penalty': round(features['volatility_penalty'], 4),
            'upper_shadow_penalty': round(features['upper_shadow_penalty'], 4),
            'scoring_version': SCORING_VERSION,
        })

    result_df = pd.DataFrame(results)
    if not result_df.empty:
        result_df['momentum_rank_score'] = percentile_score(result_df['risk_adj_momentum'])
        result_df['period_strength_score'] = percentile_score(result_df['period_change'])
        result_df['trend_quality_score'] = pd.to_numeric(result_df['trend_r2'], errors='coerce').fillna(0).clip(0, 1)
        result_df['liquidity_score'] = pd.to_numeric(result_df['volume_score'], errors='coerce').fillna(0.5).clip(0, 1)
        result_df['sector_strength_score'] = pd.to_numeric(result_df['sector_strength'], errors='coerce').fillna(0.5).clip(0, 1)
        base_score = (
            WEIGHT_VOLUME * result_df['volume_score'] +
            WEIGHT_HOT * result_df['hot_score'] +
            WEIGHT_MOMENTUM * result_df['momentum_rank_score'] +
            WEIGHT_PERIOD_STRENGTH * result_df['period_strength_score'] +
            WEIGHT_TREND_QUALITY * result_df['trend_quality_score'] +
            WEIGHT_LIQUIDITY * result_df['liquidity_score'] +
            WEIGHT_SECTOR * result_df['sector_strength_score']
        )
        risk_penalty = (
            PENALTY_DRAWDOWN * result_df['drawdown_penalty'] +
            PENALTY_OVERHEAT * result_df['overheat_penalty'] +
            PENALTY_VOLATILITY * result_df['volatility_penalty']
        )
        result_df['base_score'] = (base_score * 100).round(2)
        result_df['risk_penalty_score'] = (risk_penalty * 100).round(2)
        result_df['final_score'] = ((base_score - risk_penalty).clip(lower=0) * 100).round(2)

        result_df.sort_values(['final_score', 'risk_adj_momentum'], ascending=False, inplace=True)
        result_df['momentum_rank'] = result_df['risk_adj_momentum'].rank(method='first', ascending=False).astype(int)
        result_df['final_rank'] = range(1, len(result_df) + 1)

    return result_df


def main():
    from database import save_scored_stocks

    today = datetime.now().strftime('%Y%m%d')

    print(f"{'='*50}")
    print(f"  短线助手 — 强势股筛选")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  K线数据: mootdx(TCP) 主 / akshare 备")
    print(f"  存储: SQLite (data/shortais.db)")
    print(f"{'='*50}")

    # Phase A: 交集 + 综合评分
    print("\n>>> Phase A: 资金×人气 交集筛选")
    print(f"  成交额范围: 前{VOLUME_TOP_N}")
    print(f"  热度范围:   前{HOT_TOP_N}")
    volume_df, hot_df = load_raw_data(today)
    candidates = compute_composite_score(volume_df, hot_df)
    print(f"  综合评分Top{FINAL_TOP_N}:")
    for _, r in candidates.head(10).iterrows():
        print(f"    {r['composite_rank']:2d}. {r['code']} {r['name']}  "
              f"综合={r['composite_score']:.4f}")

    # Phase B: 动量评分
    print(f"\n>>> Phase B: {MOMENTUM_DAYS}日线性回归动量评分")
    final_df = score_momentum_batch(candidates, today)

    final_top = final_df.head(MOMENTUM_TOP_N).reset_index(drop=True)
    final_top['final_rank'] = range(1, len(final_top) + 1)

    # ---- 保存到数据库 ----
    save_scored_stocks(today, final_df, final_top)
    print(f"\n[OK] 评分结果已存入数据库")

    print(f"\n{'='*50}")
    print(f"  === 最终TOP{MOMENTUM_TOP_N} 强势股:")
    print(f"{'='*50}")
    for _, r in final_top.iterrows():
        print(f"  {r['final_rank']:2d}. {r['code']} {r['name']:>8s}  "
              f"最终={r.get('final_score', 0):.2f}  综合={r['composite_score']:.4f}  动量={r['momentum_score']:.2f}")


if __name__ == '__main__':
    main()

"""
行业板块强度模块
1. 拉取行业板块列表
2. 拉取每个行业的成分股
3. 结合全市场行情计算板块强度
"""
from __future__ import annotations

from datetime import datetime, timedelta
import re
import time
import warnings

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings('ignore')

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except Exception:
    AKSHARE_AVAILABLE = False

try:
    import pywencai
    PYWENCAI_AVAILABLE = True
except Exception:
    PYWENCAI_AVAILABLE = False

from config import SECTOR_CACHE_TTL_HOURS, LIMIT_UP_THRESHOLD
from database import load_sector_cache, save_sector_cache

CLS_HOT_PLATE_URL = "https://x-quote.cls.cn/web_quote/plate/hot_plate"


def normalize_stock_code(value) -> str | None:
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
    return digits.zfill(6)[:6]


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _request_json(url: str, params: dict | None = None) -> dict:
    """不使用系统代理，避免本地代理异常影响财联社/东财接口。"""
    session = requests.Session()
    session.trust_env = False
    resp = session.get(
        url,
        params=params or {},
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.cls.cn/quotation',
            'Origin': 'https://www.cls.cn',
        },
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def _safe_pct(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors='coerce').fillna(0)


def _is_limit_up(pct: float, code: str) -> bool:
    if pd.isna(pct):
        return False
    code = str(code)
    if code.startswith(('30', '68')):
        return pct >= 19.5
    if code.startswith('8'):
        return pct >= 29.5
    if code.startswith('4'):
        return pct >= 29.5
    return pct >= LIMIT_UP_THRESHOLD


def _fetch_industry_baskets() -> pd.DataFrame:
    if not AKSHARE_AVAILABLE:
        return pd.DataFrame()
    funcs = [
        getattr(ak, 'stock_board_industry_name_em', None),
        getattr(ak, 'stock_board_industry_name_ths', None),
    ]
    for fn in funcs:
        if fn is None:
            continue
        try:
            df = fn()
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def _fetch_industry_members(industry_name: str) -> pd.DataFrame:
    if not AKSHARE_AVAILABLE:
        return pd.DataFrame()
    funcs = [
        getattr(ak, 'stock_board_industry_cons_em', None),
        getattr(ak, 'stock_board_industry_cons_ths', None),
    ]
    for fn in funcs:
        if fn is None:
            continue
        try:
            if 'ths' in getattr(fn, '__name__', ''):
                df = fn(symbol=industry_name)
            else:
                df = fn(symbol=industry_name)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    return pd.DataFrame()


def fetch_stock_industry_wencai(code: str, name: str = '') -> str:
    """通过问财查询单只股票所属行业，作为板块接口失败时的兜底。"""
    if not PYWENCAI_AVAILABLE:
        return ''
    query_key = str(code).zfill(6)
    queries = [f'{query_key} 所属行业']
    if name:
        queries.append(f'{name} 所属行业')
    for query in queries:
        try:
            df = pywencai.get(query=query, loop=True)
            if df is None or df.empty:
                continue
            for col in df.columns:
                col_s = str(col)
                if any(k in col_s for k in ['所属同花顺行业', '所属行业', '行业']):
                    value = df[col].dropna()
                    if not value.empty:
                        raw = str(value.iloc[0]).strip()
                        if raw and raw.lower() != 'nan':
                            return raw.split(';')[0].split(',')[0].split('，')[0]
        except Exception:
            continue
    return ''


def infer_industry_by_name(name: str) -> str:
    """接口不可用时的轻量行业兜底，仅用于展示和候选池内共振。"""
    n = str(name or '')
    rules = [
        ('PCB', ['生益', '沪电', '深南', '景旺', '胜宏', '鹏鼎', '崇达', '风华']),
        ('光通信', ['亨通', '中天', '烽火', '光迅', '新易盛', '天孚', '太辰光', '永鼎']),
        ('半导体', ['兆易', '长电', '韦尔', '中芯', '华虹', '北方华创', '寒武纪', '海光']),
        ('AI服务器', ['工业富联', '浪潮信息', '中际旭创']),
        ('消费电子', ['蓝思', '立讯', '歌尔', '东山精密', '领益']),
        ('面板显示', ['京东方', 'TCL', '深天马', '维信诺']),
        ('有色金属', ['中钨', '金钼', '洛阳钼业', '紫金', '赣锋', '天齐', '云南锗业', '锗业', '锗']),
        ('化工材料', ['昊华', '多氟多', '万华', '华鲁恒升', '巨化']),
        ('医药', ['同仁堂', '恒瑞', '药明', '片仔癀', '威尔药业']),
        ('农牧食品', ['华统', '巨星农牧', '牧原', '温氏', '新希望']),
        ('银行', ['银行', '张家港行', '招商', '平安银行']),
        ('房地产', ['居然', '万科', '保利']),
    ]
    for industry, keywords in rules:
        if any(k in n for k in keywords):
            return industry
    return ''


def _fetch_spot() -> pd.DataFrame:
    if not AKSHARE_AVAILABLE:
        return pd.DataFrame()
    try:
        return ak.stock_zh_a_spot_em()
    except Exception:
        return pd.DataFrame()


def fetch_cls_hot_plates() -> pd.DataFrame:
    """获取财联社热门行业/概念/地域板块。"""
    try:
        payload = _request_json(
            CLS_HOT_PLATE_URL,
            params={'type': 'industry,concept,area', 'way': 'change', 'rever': 1},
        )
    except Exception:
        return pd.DataFrame()

    if payload.get('code') != 200 or not isinstance(payload.get('data'), dict):
        return pd.DataFrame()

    rows: list[dict] = []
    for plate_type, items in payload.get('data', {}).items():
        if not isinstance(items, list):
            continue
        for idx, item in enumerate(items):
            up_stock = item.get('up_stock') or []
            up_codes = []
            up_names = []
            up_changes = []
            for stock in up_stock:
                up_codes.append(normalize_stock_code(stock.get('secu_code')))
                up_names.append(str(stock.get('secu_name', '') or ''))
                try:
                    up_changes.append(float(stock.get('change') or 0))
                except Exception:
                    up_changes.append(0.0)
            rows.append({
                'board_name': str(item.get('secu_name', '') or ''),
                'board_code': str(item.get('secu_code', '') or ''),
                'board_type': plate_type,
                'board_change': float(item.get('change') or 0) * 100,
                'main_fund_diff': float(item.get('main_fund_diff') or 0),
                'hot_rank': idx + 1,
                'up_stock_codes': ','.join([c for c in up_codes if c]),
                'up_stock_names': ','.join([n for n in up_names if n]),
                'up_stock_max_change': max(up_changes) * 100 if up_changes else 0.0,
            })

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df['change_score'] = pd.to_numeric(df['board_change'], errors='coerce').fillna(0).rank(pct=True)
    df['fund_score'] = pd.to_numeric(df['main_fund_diff'], errors='coerce').fillna(0).rank(pct=True)
    df['leader_score'] = pd.to_numeric(df['up_stock_max_change'], errors='coerce').fillna(0).rank(pct=True)
    df['rank_score'] = 1 - (pd.to_numeric(df['hot_rank'], errors='coerce').fillna(len(df)) - 1) / max(len(df) - 1, 1)
    df['board_strength'] = (
        0.40 * df['rank_score'] +
        0.30 * df['change_score'] +
        0.20 * df['leader_score'] +
        0.10 * df['fund_score']
    ).round(4)
    df['source'] = 'cls'
    df.sort_values('board_strength', ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _name_matches_board(industry_name: str, board_name: str) -> bool:
    industry_name = str(industry_name or '')
    board_name = str(board_name or '')
    if not industry_name or not board_name:
        return False
    if industry_name in board_name or board_name in industry_name:
        return True
    aliases = {
        'PCB': ['PCB', '铜缆', '覆铜板', '印制电路板', '电子'],
        '光通信': ['通信', '光通信', '光模块', 'CPO', '光学光电子'],
        '半导体': ['半导体', '芯片', '集成电路', '先进封装'],
        '消费电子': ['消费电子', '触摸屏', '苹果概念'],
        '面板显示': ['面板', '显示', 'OLED', 'MiniLED', '光学光电子'],
        '有色金属': ['有色', '小金属', '稀土', '钴', '锂', '金属'],
        '化工材料': ['化工', '氟化工', '电子化学品', '材料'],
        'AI服务器': ['AI', '服务器', '算力', '液冷'],
    }
    keys = aliases.get(industry_name, [industry_name])
    return any(k and k in board_name for k in keys)


def build_cls_stock_sector_map(candidates: pd.DataFrame, date_str: str, force_refresh: bool = False) -> dict[str, dict]:
    """用财联社热门板块给候选股匹配板块。"""
    cls_df = fetch_cls_hot_plates()
    if cls_df.empty:
        return {}

    mapping: dict[str, dict] = {}
    for _, row in candidates.iterrows():
        code = normalize_stock_code(row.get('code'))
        name = str(row.get('name', '') or '')
        inferred = infer_industry_by_name(name)
        best = None
        best_score = -1.0
        for _, plate in cls_df.iterrows():
            up_codes = str(plate.get('up_stock_codes', '') or '').split(',')
            up_names = str(plate.get('up_stock_names', '') or '').split(',')
            board_name = str(plate.get('board_name', '') or '')
            score = float(plate.get('board_strength', 0) or 0)
            matched = False
            if code and code in up_codes:
                matched = True
                score += 0.15
            elif name and name in up_names:
                matched = True
                score += 0.15
            elif inferred and _name_matches_board(inferred, board_name):
                matched = True
            if matched and score > best_score:
                best_score = score
                best = plate
        if code and best is not None:
            mapping[code] = {
                'sector_name': str(best.get('board_name', '') or '未知'),
                'sector_strength': float(min(max(best_score, 0.0), 1.0)),
                'sector_change': float(best.get('board_change', 0) or 0),
                'sector_rise_ratio': 0.0,
                'sector_limit_up_count': 0,
                'sector_source': 'cls',
            }
    return mapping


def _compute_board_strength(board_name: str, member_codes: list[str], spot_df: pd.DataFrame) -> dict:
    if spot_df.empty or not member_codes:
        return {
            'board_name': board_name,
            'board_code': '',
            'board_change': 0.0,
            'rise_ratio': 0.0,
            'limit_up_count': 0,
            'turnover_amount': 0.0,
            'turnover_rank_score': 0.0,
            'board_strength': 0.0,
        }

    code_col = _pick_col(spot_df, ['代码', 'code'])
    pct_col = _pick_col(spot_df, ['涨跌幅', 'pct_change'])
    amount_col = _pick_col(spot_df, ['成交额', 'volume_amount'])
    if code_col is None or pct_col is None:
        return {
            'board_name': board_name,
            'board_code': '',
            'board_change': 0.0,
            'rise_ratio': 0.0,
            'limit_up_count': 0,
            'turnover_amount': 0.0,
            'turnover_rank_score': 0.0,
            'board_strength': 0.0,
        }

    temp = spot_df[[code_col, pct_col] + ([amount_col] if amount_col else [])].copy()
    temp['code'] = temp[code_col].map(normalize_stock_code)
    temp['pct_change'] = _safe_pct(temp[pct_col])
    if amount_col:
        temp['amount'] = pd.to_numeric(temp[amount_col], errors='coerce').fillna(0)
    else:
        temp['amount'] = 0.0

    temp = temp[temp['code'].isin(member_codes)]
    if temp.empty:
        return {
            'board_name': board_name,
            'board_code': '',
            'board_change': 0.0,
            'rise_ratio': 0.0,
            'limit_up_count': 0,
            'turnover_amount': 0.0,
            'turnover_rank_score': 0.0,
            'board_strength': 0.0,
        }

    rise_ratio = float((temp['pct_change'] > 0).mean())
    limit_up_count = int(sum(_is_limit_up(float(v), c) for v, c in zip(temp['pct_change'], temp['code'])))
    board_change = float(temp['pct_change'].mean())
    turnover_amount = float(temp['amount'].sum())

    return {
        'board_name': board_name,
        'board_code': '',
        'board_change': board_change,
        'rise_ratio': rise_ratio,
        'limit_up_count': limit_up_count,
        'turnover_amount': turnover_amount,
        'turnover_rank_score': 0.0,
        'board_strength': 0.0,
    }


def compute_sector_strength(date_str: str, force_refresh: bool = False) -> pd.DataFrame:
    """
    计算行业板块强度。
    返回列:
      board_name, board_change, rise_ratio, limit_up_count, turnover_amount, board_strength
    """
    if not force_refresh:
        cached = load_sector_cache(date_str)
        if cached is not None:
            return pd.DataFrame(cached)

    if not AKSHARE_AVAILABLE:
        return pd.DataFrame()

    boards = _fetch_industry_baskets()
    if boards.empty:
        return pd.DataFrame()

    name_col = _pick_col(boards, ['板块名称', '名称', '行业名称'])
    code_col = _pick_col(boards, ['板块代码', '代码', '行业代码'])
    change_col = _pick_col(boards, ['涨跌幅', '板块涨跌幅'])
    if name_col is None:
        return pd.DataFrame()

    spot_df = _fetch_spot()
    sectors: list[dict] = []
    for _, row in boards.iterrows():
        board_name = str(row.get(name_col, '')).strip()
        board_code = str(row.get(code_col, '')).strip() if code_col else ''
        member_df = _fetch_industry_members(board_name)
        if member_df.empty:
            continue

        member_code_col = _pick_col(member_df, ['代码', 'code'])
        if member_code_col is None:
            continue
        member_codes = [normalize_stock_code(v) for v in member_df[member_code_col].tolist()]
        member_codes = [c for c in member_codes if c]
        stats = _compute_board_strength(board_name, member_codes, spot_df)
        stats['board_code'] = board_code
        if change_col and change_col in boards.columns:
            stats['board_change'] = float(pd.to_numeric(row.get(change_col, 0), errors='coerce') or 0)
        sectors.append(stats)
        time.sleep(0.12)

    df = pd.DataFrame(sectors)
    if df.empty:
        return df

    df['change_score'] = pd.to_numeric(df['board_change'], errors='coerce').fillna(0).rank(pct=True)
    df['rise_score'] = pd.to_numeric(df['rise_ratio'], errors='coerce').fillna(0).rank(pct=True)
    df['limit_score'] = pd.to_numeric(df['limit_up_count'], errors='coerce').fillna(0).rank(pct=True)
    df['turnover_rank_score'] = pd.to_numeric(df['turnover_amount'], errors='coerce').fillna(0).rank(pct=True)
    df['board_strength'] = (
        0.35 * df['change_score'] +
        0.25 * df['rise_score'] +
        0.20 * df['limit_score'] +
        0.20 * df['turnover_rank_score']
    ).round(4)
    df.sort_values('board_strength', ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)

    save_sector_cache(date_str, df.to_dict('records'))
    return df


def build_stock_sector_map(sector_df: pd.DataFrame) -> dict[str, dict]:
    """按行业强度表构建股票到行业的映射。失败时返回空字典。"""
    if sector_df.empty or not AKSHARE_AVAILABLE or 'board_name' not in sector_df.columns:
        return {}
    mapping: dict[str, dict] = {}
    for _, sector in sector_df.iterrows():
        board_name = str(sector.get('board_name', '')).strip()
        if not board_name:
            continue
        members = _fetch_industry_members(board_name)
        if members.empty:
            continue
        code_col = _pick_col(members, ['代码', 'code'])
        if code_col is None:
            continue
        payload = {
            'sector_name': board_name,
            'sector_strength': float(sector.get('board_strength', 0) or 0),
            'sector_change': float(sector.get('board_change', 0) or 0),
            'sector_rise_ratio': float(sector.get('rise_ratio', 0) or 0),
            'sector_limit_up_count': int(sector.get('limit_up_count', 0) or 0),
        }
        for code in members[code_col].map(normalize_stock_code).dropna():
            existing = mapping.get(code)
            if existing is None or payload['sector_strength'] > existing.get('sector_strength', 0):
                mapping[code] = payload
        time.sleep(0.08)
    return mapping


def neutral_sector_payload() -> dict:
    """板块数据不可用时使用中性值，避免阻断主流程。"""
    return {
        'sector_name': '未知',
        'sector_strength': 0.5,
        'sector_change': 0.0,
        'sector_rise_ratio': 0.0,
        'sector_limit_up_count': 0,
        'sector_source': 'fallback',
    }

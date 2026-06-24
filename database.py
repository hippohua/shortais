"""
短线助手 — SQLite 数据库模块
以日期为维度存储原始数据、评分结果和图表缓存。
当天数据已存在时自动跳过接口调用，大幅减少 API 请求频率。
"""
import sqlite3
import json
import pandas as pd
from pathlib import Path
from config import OUTPUT_DIR, SCORING_VERSION

DB_PATH = str(Path(OUTPUT_DIR) / 'shortais.db')


def get_conn() -> sqlite3.Connection:
    """获取数据库连接"""
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表（幂等操作）
    chart_cache 手动建表；raw_volume/raw_hot/scored_stocks 由 pandas to_sql() 首次写入时自动建表。
    如果存在之前运行遗留的空占位表（仅含 date 列），先删除它们。"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chart_cache (
            date TEXT PRIMARY KEY,
            chart_data TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_cache (
            date TEXT PRIMARY KEY,
            sector_data TEXT NOT NULL
        )
    """)
    # 排名第一历史记录表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rank1_history (
            date TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            momentum_score REAL,
            final_score REAL,
            composite_score REAL,
            trend_slope REAL,
            trend_r2 REAL,
            period_change REAL
        )
    """)
    # 清理可能残留的空占位表（列数 ≤2），让 to_sql() 重新创建完整 schema
    for tbl in ['raw_volume', 'raw_hot', 'scored_stocks']:
        try:
            cols = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            if len(cols) <= 2:  # 只有 date ± sheet 两列的占位表，无实际数据
                conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        except sqlite3.OperationalError:
            pass  # 表不存在，无需处理
    conn.commit()
    conn.close()


# ==================== 原始数据（成交额榜 + 热度榜） ====================

def has_raw_data(date: str) -> bool:
    """检查当天原始数据是否已入库"""
    conn = get_conn()
    try:
        v = conn.execute("SELECT COUNT(*) as cnt FROM raw_volume WHERE date = ?", (date,)).fetchone()
        h = conn.execute("SELECT COUNT(*) as cnt FROM raw_hot WHERE date = ?", (date,)).fetchone()
        return v['cnt'] > 0 and h['cnt'] > 0
    except Exception:
        return False
    finally:
        conn.close()


def _ensure_table_columns(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> None:
    """确保表 schema 包含 DataFrame 的所有列，缺失的自动 ALTER TABLE 补齐"""
    try:
        existing_cols = {c[1] for c in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return  # 表不存在，后续 to_sql 会创建
    for col in df.columns:
        if col not in existing_cols:
            # 推断类型：float 用 REAL，其他用 TEXT
            dtype = df[col].dtype
            if 'float' in str(dtype) or 'int' in str(dtype):
                sql_type = 'REAL'
            else:
                sql_type = 'TEXT'
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {sql_type}')


def save_raw_volume(date: str, df: pd.DataFrame) -> None:
    """保存成交额排名数据（先删后插，确保幂等）"""
    conn = get_conn()
    df = df.copy()
    df['date'] = date
    _ensure_table_columns(conn, 'raw_volume', df)
    try:
        conn.execute("DELETE FROM raw_volume WHERE date = ?", (date,))
    except sqlite3.OperationalError:
        pass  # 表不存在，to_sql 会自动建表
    df.to_sql('raw_volume', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()


def save_raw_hot(date: str, df: pd.DataFrame) -> None:
    """保存热度排名数据（先删后插，确保幂等）"""
    conn = get_conn()
    df = df.copy()
    df['date'] = date
    _ensure_table_columns(conn, 'raw_hot', df)
    try:
        conn.execute("DELETE FROM raw_hot WHERE date = ?", (date,))
    except sqlite3.OperationalError:
        pass  # 表不存在，to_sql 会自动建表
    df.to_sql('raw_hot', conn, if_exists='append', index=False)
    conn.commit()
    conn.close()


def load_raw_volume(date: str) -> pd.DataFrame:
    """从数据库读取成交额排名"""
    conn = get_conn()
    try:
        df = pd.read_sql_query("SELECT * FROM raw_volume WHERE date = ?", conn, params=(date,))
        return df.drop(columns=['date'], errors='ignore') if not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def load_raw_hot(date: str) -> pd.DataFrame:
    """从数据库读取热度排名"""
    conn = get_conn()
    try:
        df = pd.read_sql_query("SELECT * FROM raw_hot WHERE date = ?", conn, params=(date,))
        return df.drop(columns=['date'], errors='ignore') if not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


# ==================== 评分结果 ====================

def has_scored_data(date: str) -> bool:
    """检查当天评分数据是否已入库"""
    conn = get_conn()
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(scored_stocks)").fetchall()}
        if 'scoring_version' not in cols:
            return False
        c = conn.execute(
            "SELECT COUNT(*) as cnt FROM scored_stocks WHERE date = ? AND scoring_version = ?",
            (date, SCORING_VERSION),
        )
        return c.fetchone()['cnt'] > 0
    except Exception:
        return False
    finally:
        conn.close()


def save_scored_stocks(date: str, final_df: pd.DataFrame, final_top: pd.DataFrame) -> None:
    """保存评分结果（全部候选 + TOP N）
    自动补全缺失列（如 final_rank），兼容表已存在但 schema 不完整的情况"""
    conn = get_conn()
    df_top = final_top.copy()
    df_top['date'] = date
    df_top['sheet'] = 'top'
    df_top['scoring_version'] = SCORING_VERSION
    df_all = final_df.copy()
    df_all['date'] = date
    df_all['sheet'] = 'all'
    df_all['scoring_version'] = SCORING_VERSION

    # 先删当天旧数据
    for sheet in ['top', 'all']:
        try:
            conn.execute("DELETE FROM scored_stocks WHERE date = ? AND sheet = ?", (date, sheet))
        except sqlite3.OperationalError:
            pass

    # 确保表 schema 包含所有列：检测缺失列并 ALTER TABLE 补齐
    existing_cols = {c[1] for c in conn.execute("PRAGMA table_info(scored_stocks)").fetchall()}
    needed_cols = set(df_top.columns) | set(df_all.columns)
    if existing_cols:
        for col in needed_cols - existing_cols:
            conn.execute(f"ALTER TABLE scored_stocks ADD COLUMN \"{col}\"")

    # 先写 final_top（含 final_rank，建完整 schema），再写 final_df（缺 final_rank 列自动 NULL）
    df_top.to_sql('scored_stocks', conn, if_exists='append', index=False)
    df_all.to_sql('scored_stocks', conn, if_exists='append', index=False)

    conn.commit()
    conn.close()


def load_scored_all(date: str) -> pd.DataFrame:
    """读取全部候选评分数据"""
    conn = get_conn()
    try:
        df = pd.read_sql_query(
            "SELECT * FROM scored_stocks WHERE date = ? AND sheet = 'all'", conn, params=(date,)
        )
        return df.drop(columns=['date', 'sheet'], errors='ignore') if not df.empty else pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def load_scored_top(date: str, top_n: int = 10) -> pd.DataFrame:
    """读取动量排序 TOP N"""
    conn = get_conn()
    try:
        cols = {c[1] for c in conn.execute("PRAGMA table_info(scored_stocks)").fetchall()}
        order_col = 'final_score' if 'final_score' in cols else 'momentum_score'
        df = pd.read_sql_query(
            "SELECT * FROM scored_stocks WHERE date = ? AND sheet = 'top' "
            f"ORDER BY {order_col} DESC LIMIT ?",
            conn, params=(date, top_n)
        )
        result = df.drop(columns=['date', 'sheet'], errors='ignore') if not df.empty else pd.DataFrame()
        if not result.empty:
            result = result.reset_index(drop=True)
            result['final_rank'] = range(1, len(result) + 1)
        return result
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


# ==================== 图表缓存 ====================

def has_chart_cache(date: str) -> bool:
    """检查图表缓存是否存在"""
    conn = get_conn()
    try:
        c = conn.execute("SELECT COUNT(*) as cnt FROM chart_cache WHERE date = ?", (date,))
        return c.fetchone()['cnt'] > 0
    except Exception:
        return False
    finally:
        conn.close()


def save_chart_cache(date: str, chart_data: list[dict]) -> None:
    """保存图表数据（JSON blob）"""
    conn = get_conn()
    json_str = json.dumps(chart_data, ensure_ascii=False)
    conn.execute(
        "INSERT OR REPLACE INTO chart_cache (date, chart_data) VALUES (?, ?)",
        (date, json_str)
    )
    conn.commit()
    conn.close()


def load_chart_cache(date: str) -> list[dict] | None:
    """读取图表缓存"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT chart_data FROM chart_cache WHERE date = ?", (date,)).fetchone()
        if row:
            return json.loads(row['chart_data'])
    except (json.JSONDecodeError, Exception):
        pass
    finally:
        conn.close()
    return None


def clear_derived_cache(date: str) -> None:
    """清理当天评分和图表缓存，保留原始数据。"""
    conn = get_conn()
    for sql, params in [
        ("DELETE FROM scored_stocks WHERE date = ?", (date,)),
        ("DELETE FROM chart_cache WHERE date = ?", (date,)),
        ("DELETE FROM sector_cache WHERE date = ?", (date,)),
    ]:
        try:
            conn.execute(sql, params)
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


def has_sector_cache(date: str) -> bool:
    """检查板块缓存是否存在。"""
    conn = get_conn()
    try:
        c = conn.execute("SELECT COUNT(*) as cnt FROM sector_cache WHERE date = ?", (date,)).fetchone()
        return c['cnt'] > 0
    except Exception:
        return False
    finally:
        conn.close()


def save_sector_cache(date: str, sector_data: list[dict]) -> None:
    """保存板块强度缓存。"""
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO sector_cache (date, sector_data) VALUES (?, ?)",
        (date, json.dumps(sector_data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def load_sector_cache(date: str) -> list[dict] | None:
    """读取板块强度缓存。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT sector_data FROM sector_cache WHERE date = ?", (date,)).fetchone()
        if row:
            return json.loads(row['sector_data'])
    except Exception:
        pass
    finally:
        conn.close()
    return None


# ==================== 大盘/板块/涨跌停缓存（dcsdk） ====================

def save_market_summary(date: str, data: dict) -> None:
    """保存大盘行情摘要（JSON）"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_summary (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO market_summary (date, data) VALUES (?, ?)",
        (date, json.dumps(data, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def load_market_summary(date: str) -> dict | None:
    """读取大盘行情摘要"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT data FROM market_summary WHERE date = ?", (date,)).fetchone()
        if row:
            return json.loads(row['data'])
    except Exception:
        pass
    finally:
        conn.close()
    return None


def save_board_data(date: str, boards: list) -> None:
    """保存行业板块数据"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS board_data (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO board_data (date, data) VALUES (?, ?)",
        (date, json.dumps(boards, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def load_board_data(date: str) -> list | None:
    """读取行业板块数据"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT data FROM board_data WHERE date = ?", (date,)).fetchone()
        if row:
            return json.loads(row['data'])
    except Exception:
        pass
    finally:
        conn.close()
    return None


def save_limit_data(date: str, limits: dict) -> None:
    """保存涨跌停监控数据"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limit_data (
            date TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT OR REPLACE INTO limit_data (date, data) VALUES (?, ?)",
        (date, json.dumps(limits, ensure_ascii=False))
    )
    conn.commit()
    conn.close()


def load_limit_data(date: str) -> dict | None:
    """读取涨跌停监控数据"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT data FROM limit_data WHERE date = ?", (date,)).fetchone()
        if row:
            return json.loads(row['data'])
    except Exception:
        pass
    finally:
        conn.close()
    return None


# ==================== 工具函数 ====================

def get_latest_date() -> str | None:
    """获取最近一次评分日期"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT MAX(date) as max_date FROM scored_stocks").fetchone()
        if row and row['max_date']:
            return row['max_date']
    except Exception:
        pass
    finally:
        conn.close()
    return None


def get_raw_stats(date: str) -> dict:
    """获取当天原始数据的统计信息（用于前端展示）"""
    conn = get_conn()
    try:
        v = conn.execute("SELECT COUNT(*) as cnt FROM raw_volume WHERE date = ?", (date,)).fetchone()
        h = conn.execute("SELECT COUNT(*) as cnt FROM raw_hot WHERE date = ?", (date,)).fetchone()
        return {'volume_count': v['cnt'], 'hot_count': h['cnt']}
    except Exception:
        return {'volume_count': 0, 'hot_count': 0}
    finally:
        conn.close()


# ==================== 近7日排名第一历史 ====================

def init_rank1_table() -> None:
    """创建 rank1_history 表（幂等）"""
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rank1_history (
            date TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            momentum_score REAL NOT NULL,
            final_score REAL,
            period_change REAL DEFAULT 0,
            date_display TEXT NOT NULL
        )
    """)
    cols = {c[1] for c in conn.execute("PRAGMA table_info(rank1_history)").fetchall()}
    if 'final_score' not in cols:
        conn.execute("ALTER TABLE rank1_history ADD COLUMN final_score REAL")
    if 'period_change' not in cols:
        conn.execute("ALTER TABLE rank1_history ADD COLUMN period_change REAL DEFAULT 0")
    if 'date_display' not in cols:
        conn.execute("ALTER TABLE rank1_history ADD COLUMN date_display TEXT")
    conn.commit()
    conn.close()


def save_rank1(date: str, row: dict) -> None:
    """保存当日排名第一的股票信息"""
    init_rank1_table()
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO rank1_history (date, code, name, momentum_score, final_score, period_change, date_display)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (date, row['code'], row['name'], row['momentum_score'], row.get('final_score', row['momentum_score']),
          row.get('period_change', 0), row.get('date_display', date)))
    conn.commit()
    conn.close()


def load_rank1_history(days: int = 7) -> list[dict]:
    """读取近N天排名第一的历史记录"""
    init_rank1_table()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM rank1_history ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()

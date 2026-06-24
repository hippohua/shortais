"""
短线助手 — Web 应用
启动后自动打开浏览器，显示最近一次运行结果
点击"重新运行"按顺序执行：数据获取 → 筛选评分 → 结果展示
存储: SQLite 数据库，当天数据已入库则自动跳过接口调用
"""
import json
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, TypedDict

import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

from flask import Flask, Response, jsonify, request

from config import (
    MOMENTUM_DAYS, MOMENTUM_TOP_N, KLINE_DAYS, OUTPUT_DIR
)

# ──────────────────────────────────────────────
# 数据库模块
# ──────────────────────────────────────────────
import database as db

# ──────────────────────────────────────────────
# 导入各模块函数
# ──────────────────────────────────────────────
from get_data import fetch_data as _fetch_raw_data
from get_data import (
    fetch_market_summary, fetch_board_industry, fetch_board_concept,
    fetch_limit_scan, DCSDK_AVAILABLE as _DCSDK_AVAILABLE,
)
from screener import (
    load_raw_data, compute_composite_score, score_momentum_batch,
    normalize_stock_code, attach_sector_strength,
)
from visualizer import (
    fetch_kline_data, compute_momentum_for_display, build_kline_echarts_data
)

# ──────────────────────────────────────────────
# Flask 初始化
# ──────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────
# 全局状态（线程安全）
# ──────────────────────────────────────────────
class PipelineState(TypedDict):
    running: bool
    stage: str
    progress: int
    total_stages: int
    log: list[str]
    result_date: Optional[str]
    error: Optional[str]
    # 新增：中间步骤结果存储
    step_results: dict[str, Any]


_pipeline_lock = threading.Lock()
_pipeline_state: PipelineState = {
    'running': False,
    'stage': '',
    'progress': 0,
    'total_stages': 4,
    'log': [],
    'result_date': None,
    'error': None,
    'step_results': {},
}


def _set_state(**kwargs: Any) -> None:
    with _pipeline_lock:
        for k, v in kwargs.items():
            _pipeline_state[k] = v  # type: ignore[index,literal-required]


def _add_log(msg: str) -> None:
    with _pipeline_lock:
        _pipeline_state['log'].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
        if len(_pipeline_state['log']) > 200:
            _pipeline_state['log'] = _pipeline_state['log'][-200:]


def _get_state() -> dict[str, Any]:
    with _pipeline_lock:
        return dict(_pipeline_state)


def _store_step_result(step_name: str, data: Any) -> None:
    """存储中间步骤结果到状态"""
    with _pipeline_lock:
        _pipeline_state['step_results'][step_name] = data


def _get_step_result(step_name: str) -> Any:
    """获取中间步骤结果"""
    with _pipeline_lock:
        return _pipeline_state['step_results'].get(step_name)


# ──────────────────────────────────────────────
# 工具函数：查找最近一次运行
# ──────────────────────────────────────────────
def find_latest_run() -> Optional[str]:
    """从数据库查询最近一次评分日期"""
    return db.get_latest_date()


# ──────────────────────────────────────────────
# 生成并缓存图表数据
# ──────────────────────────────────────────────
def generate_chart_data(date_str: str, top_df: Optional[pd.DataFrame] = None) -> list[dict[str, Any]]:
    """为指定日期的 TOP 股票生成图表数据，返回 stocks JSON 列表
    当 top_df 不为 None 时直接使用内存数据（避免 xlsx 来回读造成的数值精度漂移）
    图表数据会缓存到数据库，当天第二次运行直接读取"""
    # 1. 先查数据库缓存
    cached = db.load_chart_cache(date_str)
    if top_df is None and cached is not None:
        return cached

    # 2. 加载评分数据
    if top_df is not None:
        pass
    else:
        top_df = db.load_scored_top(date_str, MOMENTUM_TOP_N)
        if top_df.empty:
            return []
    stocks: list[dict[str, Any]] = []

    for row_idx, (_, row) in enumerate(top_df.iterrows()):
        code = str(row.get('code', '')).zfill(6)
        name = str(row.get('name', ''))
        rank = row_idx + 1
        composite = float(row.get('composite_score', 0) or 0)  # type: ignore[arg-type]
        momentum = float(row.get('momentum_score', 0) or 0)  # type: ignore[arg-type]
        final_score = float(row.get('final_score', momentum) or 0)  # type: ignore[arg-type]
        slope = float(row.get('trend_slope', 0) or 0)  # type: ignore[arg-type]
        r2 = float(row.get('trend_r2', 0) or 0)  # type: ignore[arg-type]

        # K线数据
        kline_df = fetch_kline_data(code, KLINE_DAYS, date_str)
        echarts_data = build_kline_echarts_data(kline_df) if not kline_df.empty else {}

        # 动量数据
        hist = fetch_kline_data(code, MOMENTUM_DAYS, date_str)
        if not hist.empty and '收盘' in hist.columns:
            close_prices = hist['收盘']
            mom_data = compute_momentum_for_display(close_prices)
        else:
            mom_data = {
                'days': [0], 'prices': [1], 'relative': [1],
                'trend_line': [1], 'momentum': 0, 'slope': 0, 'r2': 0
            }

        # 期间涨跌幅
        period_change = 0.0
        if not hist.empty and '收盘' in hist.columns and len(hist) >= 2:
            first_close = float(hist['收盘'].iloc[0])
            last_close = float(hist['收盘'].iloc[-1])
            if first_close > 0:
                period_change = round((last_close / first_close - 1) * 100, 2)

        mom_json = {
            'days': mom_data['days'],
            'relative': mom_data['relative'],
            'trend_line': mom_data['trend_line'],
            'prices': mom_data['prices'],
        }

        stocks.append({
            'rank': rank,
            'code': code,
            'name': name,
            'composite': round(composite, 4),
            'momentum': round(momentum, 2),
            'final_score': round(final_score, 2),
            'risk_adj_momentum': round(float(row.get('risk_adj_momentum', momentum) or 0), 2),
            'max_drawdown': round(float(row.get('max_drawdown', 0) or 0), 2),
            'volatility': round(float(row.get('volatility', 0) or 0), 2),
            'overheat_penalty': round(float(row.get('overheat_penalty', 0) or 0), 4),
            'candidate_mode': str(row.get('candidate_mode', 'intersection') or 'intersection'),
            'sector_name': str(row.get('sector_name', '未知') or '未知'),
            'sector_strength': round(float(row.get('sector_strength', 0.5) or 0.5), 4),
            'sector_change': round(float(row.get('sector_change', 0) or 0), 2),
            'sector_rise_ratio': round(float(row.get('sector_rise_ratio', 0) or 0), 4),
            'sector_limit_up_count': int(row.get('sector_limit_up_count', 0) or 0),
            'sector_source': str(row.get('sector_source', 'fallback') or 'fallback'),
            'slope': round(slope, 6),
            'r2': round(r2, 4),
            'period_change': period_change,
            'kline_data': echarts_data,
            'momentum_data': mom_json,
        })

    # 缓存到数据库
    db.save_chart_cache(date_str, stocks)

    return stocks


# ──────────────────────────────────────────────
# 分步流水线执行（后台线程）
# ──────────────────────────────────────────────

def _step1_fetch_data(today: str, output_dir: Path, skip_fetch: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    """步骤1: 数据获取（skip_fetch=True 时从数据库加载，不调用接口）"""
    _set_state(stage='数据获取', progress=0)

    if skip_fetch:
        _add_log('▶ [步骤1/4] 今日数据已入库，跳过数据获取（从数据库加载）')
        volume_df = db.load_raw_volume(today)
        hot_df = db.load_raw_hot(today)
        source = '数据库缓存'
    else:
        _add_log('▶ [步骤1/4] 开始数据获取...')
        volume_df, hot_df, source = _fetch_raw_data(today)
        # 保存到数据库
        db.save_raw_volume(today, volume_df)
        db.save_raw_hot(today, hot_df)

    _add_log(f'✓ 数据获取完成（数据源: {source}）')
    _add_log(f'  成交额榜 {len(volume_df)} 只，热度榜 {len(hot_df)} 只')  # type: ignore[arg-type]

    # 提取全部股票用于前端展示
    _disp_cols = ['code', 'name']
    vol_top = volume_df[_disp_cols].to_dict('records')  # type: ignore[union-attr]
    hot_top = hot_df[_disp_cols].to_dict('records')  # type: ignore[union-attr]

    # 计算交集
    vol_codes = set(volume_df['code'].map(normalize_stock_code).dropna())  # type: ignore[union-attr]
    hot_codes_set = set(hot_df['code'].map(normalize_stock_code).dropna())  # type: ignore[union-attr]
    inter_codes = vol_codes & hot_codes_set

    # 补齐交集股票名称
    code_to_name: dict[str, str] = {}
    for df_slice in [volume_df[['code', 'name']], hot_df[['code', 'name']]]:
        for _, row in df_slice.iterrows():  # type: ignore[union-attr]
            key = normalize_stock_code(row['code'])
            if key:
                code_to_name[key] = str(row['name'])

    intersection_stocks = [{'code': c, 'name': code_to_name.get(c, '')} for c in sorted(inter_codes)]

    # 存储中间结果
    _store_step_result('step1', {
        'source': source,
        'volume_count': len(vol_codes),
        'hot_count': len(hot_codes_set),
        'intersection_count': len(inter_codes),
        'volume_stocks': vol_top,
        'hot_stocks': hot_top,
        'intersection_stocks': intersection_stocks,
    })

    return volume_df, hot_df, source


def _step2_screen(today: str, skip_score: bool = False, force_refresh: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    """步骤2: 筛选与动量评分，返回 (final_df, final_top)
    skip_score=True 时从数据库加载已有评分，不重新计算"""
    _set_state(stage='筛选评分', progress=1)

    if skip_score:
        _add_log('▶ [步骤2/4] 今日评分已入库，跳过筛选评分（从数据库加载）')
        final_top = db.load_scored_top(today, MOMENTUM_TOP_N)
        final_df = db.load_scored_all(today)
        _add_log(f'✓ 从数据库加载 {len(final_df)} 只候选，TOP{len(final_top)} 只')
        # TOP 为空说明上次写入不完整，回退到重新计算
        if final_top.empty or final_df.empty:
            _add_log('  ⚠ 缓存数据不完整，回退到重新筛选评分...')
            skip_score = False
    if not skip_score:
        _add_log('▶ [步骤2/4] 开始筛选与动量评分...')
        vol_df, hot_df2 = load_raw_data(today)
        candidates = compute_composite_score(vol_df, hot_df2)
        candidates = attach_sector_strength(candidates, today, force_refresh=force_refresh)
        _add_log(f'  交集 {len(candidates)} 只，进入动量分析')

        final_df = score_momentum_batch(candidates, today)
        final_top = final_df.head(MOMENTUM_TOP_N).reset_index(drop=True)
        final_top['final_rank'] = range(1, len(final_top) + 1)

        # 保存到数据库
        db.save_scored_stocks(today, final_df, final_top)
        _add_log(f'✓ TOP{MOMENTUM_TOP_N} 筛选完成，已存入数据库')

    # 存储中间结果（SSE 前端展示用）
    step_cols = [
        'code', 'name', 'final_score', 'composite_score', 'momentum_score',
        'risk_adj_momentum', 'max_drawdown', 'overheat_penalty', 'candidate_mode',
        'sector_name', 'sector_strength', 'sector_change', 'sector_rise_ratio',
        'sector_limit_up_count', 'sector_source', 'trend_slope', 'trend_r2'
    ]
    _scored = final_top[[c for c in step_cols if c in final_top.columns]].copy()
    if 'final_score' not in _scored.columns:
        _scored['final_score'] = _scored.get('momentum_score', 0)
    _scored['composite_score'] = _scored['composite_score'].round(4).astype(float)
    _scored['final_score'] = _scored['final_score'].round(2).astype(float)
    _scored['momentum_score'] = _scored['momentum_score'].round(2).astype(float)
    if 'risk_adj_momentum' in _scored.columns:
        _scored['risk_adj_momentum'] = _scored['risk_adj_momentum'].round(2).astype(float)
    if 'max_drawdown' in _scored.columns:
        _scored['max_drawdown'] = _scored['max_drawdown'].round(2).astype(float)
    if 'overheat_penalty' in _scored.columns:
        _scored['overheat_penalty'] = _scored['overheat_penalty'].round(4).astype(float)
    for col in ['sector_strength', 'sector_change', 'sector_rise_ratio']:
        if col in _scored.columns:
            _scored[col] = pd.to_numeric(_scored[col], errors='coerce').fillna(0).round(4).astype(float)
    _scored['trend_slope'] = _scored['trend_slope'].round(6).astype(float)
    _scored['trend_r2'] = _scored['trend_r2'].round(4).astype(float)
    _store_step_result('step2', {
        'intersection_count': len(final_df),
        'final_count': len(final_df),
        'top_count': len(final_top),
        'scored_stocks': _scored.to_dict('records'),
    })

    return final_df, final_top


def _step3_generate_charts(today: str, final_top: Optional[pd.DataFrame] = None,
                           skip_chart: bool = False) -> list[dict[str, Any]]:
    """步骤3: 生成图表数据"""
    _set_state(stage='图表生成', progress=2)

    if skip_chart:
        _add_log('▶ [步骤3/4] 图表缓存已存在，跳过图表生成（从数据库加载）')
        stocks = db.load_chart_cache(today) or []
    else:
        _add_log('▶ [步骤3/4] 开始生成图表数据...')
        stocks = generate_chart_data(today, top_df=final_top)

    _add_log(f'✓ 图表数据生成完成（{len(stocks)} 只股票）')

    # 存储中间结果
    _store_step_result('step3', {
        'chart_stocks_count': len(stocks),
    })

    return stocks


def _step4_finalize(today: str, stocks: list[dict[str, Any]]) -> None:
    """步骤4: 完成并存储最终结果（含 dcsdk 大盘/板块数据）"""
    _set_state(stage='完成', progress=3, result_date=today)
    _add_log('✓✓ [步骤4/4] 全部分析完成！')

    # 非阻塞获取 dcsdk 大盘/板块数据
    if _DCSDK_AVAILABLE:
        try:
            market = fetch_market_summary()
            if market:
                db.save_market_summary(today, market)
                _add_log('  dcsdk 大盘行情已缓存')
        except Exception as e:
            _add_log(f'  [WARN] dcsdk 大盘行情获取失败: {e}')
        try:
            boards = fetch_board_industry()
            if boards:
                db.save_board_data(today, boards)
                _add_log(f'  dcsdk 行业板块已缓存（{len(boards)}个）')
        except Exception as e:
            _add_log(f'  [WARN] dcsdk 行业板块获取失败: {e}')
        try:
            limits = fetch_limit_scan()
            if limits:
                db.save_limit_data(today, limits)
                _add_log('  dcsdk 涨跌停数据已缓存')
        except Exception as e:
            _add_log(f'  [WARN] dcsdk 涨跌停数据获取失败: {e}')

    # 存储最终结果摘要
    if stocks:
        avg_momentum = round(np.mean([s['momentum'] for s in stocks]), 2)
        best = max(stocks, key=lambda x: x.get('final_score', x['momentum']))
        _store_step_result('step4', {
            'date': today,
            'total_stocks': len(stocks),
            'avg_momentum': avg_momentum,
            'best_stock': {
                'name': best['name'],
                'code': best['code'],
                'momentum': best['momentum'],
                'final_score': best.get('final_score', best['momentum']),
            },
        })
        # 保存当日排名第一的股票到历史记录
        db.save_rank1(today, {
            'code': str(best['code']),
            'name': str(best['name']),
            'momentum_score': float(best['momentum']),
            'final_score': float(best.get('final_score', best['momentum'])),
            'period_change': float(best.get('period_change', 0)),
            'date_display': datetime.strptime(today, '%Y%m%d').strftime('%m-%d'),
        })


def run_pipeline_thread(force_refresh: bool = False) -> None:
    """在后台线程中执行完整流水线（4步骤）
    当天数据已入库则自动跳过接口调用，直接从数据库加载"""
    today = datetime.now().strftime('%Y%m%d')
    output_dir = Path(OUTPUT_DIR) / today
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 检查各阶段缓存状态
        if force_refresh:
            _add_log('  已选择刷新数据并运行：将重新拉取今日成交额/热度，并重算评分与图表')
            db.clear_derived_cache(today)
            skip_fetch = False
            skip_score = False
            skip_chart = False
        else:
            skip_fetch = db.has_raw_data(today)
            skip_score = db.has_scored_data(today)
            skip_chart = db.has_chart_cache(today)
        if not skip_score:
            skip_chart = False

        skip_count = sum([skip_fetch, skip_score, skip_chart])
        if skip_count > 0:
            _add_log(f'  数据库缓存命中 {skip_count}/3 个阶段，将跳过重复计算')

        # Step 1: 数据获取（当天已有则从数据库加载）
        _step1_fetch_data(today, output_dir, skip_fetch=skip_fetch)

        # Step 2: 筛选评分（当天已有则从数据库加载）
        _, final_top = _step2_screen(today, skip_score=skip_score, force_refresh=force_refresh)

        # Step 3: 生成图表（缓存命中则直接返回）
        stocks = _step3_generate_charts(today, final_top, skip_chart=skip_chart)

        # Step 4: 完成
        _step4_finalize(today, stocks)

    except Exception as e:
        import traceback
        error_msg = f'{type(e).__name__}: {e}'
        _add_log(f'✗ 运行失败: {error_msg}')
        _add_log(traceback.format_exc())
        _set_state(stage='错误', error=error_msg)
    finally:
        _set_state(running=False)


# ──────────────────────────────────────────────
# API 路由
# ──────────────────────────────────────────────
@app.route('/api/last-run')
def api_last_run():
    """返回最近一次运行的日期"""
    latest = find_latest_run()
    if latest:
        return jsonify({'has_data': True, 'date': latest})
    return jsonify({'has_data': False, 'date': None})


@app.route('/api/rank1-history')
def api_rank1_history():
    """返回近7天每日排名第一的股票历史"""
    records = db.load_rank1_history(days=7)
    return jsonify({'records': records})


@app.route('/api/market-summary/<date_str>')
def api_market_summary(date_str):
    """返回大盘行情摘要（dcsdk）"""
    data = db.load_market_summary(date_str)
    if data:
        return jsonify({'has_data': True, 'data': data})
    return jsonify({'has_data': False, 'data': None})


@app.route('/api/board-data/<date_str>')
def api_board_data(date_str):
    """返回行业板块数据（dcsdk）"""
    data = db.load_board_data(date_str)
    if data:
        return jsonify({'has_data': True, 'data': data})
    return jsonify({'has_data': False, 'data': []})


@app.route('/api/limit-data/<date_str>')
def api_limit_data(date_str):
    """返回涨跌停监控数据（dcsdk）"""
    data = db.load_limit_data(date_str)
    if data:
        return jsonify({'has_data': True, 'data': data})
    return jsonify({'has_data': False, 'data': {}})


@app.route('/api/results/<date_str>')
def api_results(date_str):
    """返回指定日期的分析结果（含图表数据）"""
    try:
        stocks = generate_chart_data(date_str)
        if not stocks:
            return jsonify({'error': '该日期没有分析结果'}), 404

        avg_momentum = round(np.mean([s['momentum'] for s in stocks]), 2)
        avg_final = round(np.mean([s.get('final_score', s['momentum']) for s in stocks]), 2)
        best_stock = max(stocks, key=lambda x: x.get('final_score', x['momentum']))

        return jsonify({
            'date': datetime.strptime(date_str, '%Y%m%d').strftime('%Y-%m-%d'),
            'stocks': stocks,
            'stats': {
                'total': len(stocks),
                'avg_momentum': avg_momentum,
                'avg_final': avg_final,
                'best_stock': {
                    'name': best_stock['name'],
                    'code': best_stock['code'],
                    'rank': best_stock['rank'],
                    'momentum': best_stock['momentum'],
                    'final_score': best_stock.get('final_score', best_stock['momentum']),
                    'period_change': best_stock['period_change'],
                },
            },
            'momenta_days': MOMENTUM_DAYS,
            'kline_days': KLINE_DAYS,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/run', methods=['POST'])
def api_run():
    """触发重新运行"""
    state = _get_state()
    if state['running']:
        return jsonify({'error': '已有任务正在运行，请稍候'}), 409
    payload = request.get_json(silent=True) or {}
    force_refresh = bool(payload.get('force_refresh', False))

    # 重置状态
    _set_state(
        running=True,
        stage='准备中',
        progress=0,
        total_stages=4,
        log=[],
        result_date=None,
        error=None,
    )

    # 在后台线程中运行
    thread = threading.Thread(target=run_pipeline_thread, kwargs={'force_refresh': force_refresh}, daemon=True)
    thread.start()

    message = '刷新数据并分析任务已启动' if force_refresh else '分析任务已启动'
    return jsonify({'success': True, 'message': message, 'force_refresh': force_refresh})


@app.route('/api/status')
def api_status():
    """获取当前运行状态（轮询）"""
    return jsonify(_get_state())


@app.route('/api/status/stream')
def api_status_stream():
    """SSE 实时推送运行状态"""
    def generate():
        last_log_count = 0
        sent_steps: set[str] = set()
        while True:
            state = _get_state()
            logs: list[str] = state['log']
            current_log_count = len(logs)

            if current_log_count > last_log_count:
                new_logs: list[str] = logs[last_log_count:]
                for log_line in new_logs:
                    yield f"data: {json.dumps({'type': 'log', 'text': log_line}, ensure_ascii=False)}\n\n"
                last_log_count = current_log_count

            # 推送进度
            yield f"data: {json.dumps({'type': 'progress', 'stage': state['stage'], 'progress': state['progress'], 'total': state['total_stages'], 'running': state['running']}, ensure_ascii=False)}\n\n"

            # 推送步骤中间结果
            step_results: dict[str, Any] = state['step_results']
            for step_name in ['step1', 'step2']:
                if step_name in step_results and step_name not in sent_steps:
                    yield f"data: {json.dumps({'type': 'step_data', 'step': step_name, 'data': step_results[step_name]}, ensure_ascii=False)}\n\n"
                    sent_steps.add(step_name)

            if not state['running']:
                if state['error']:
                    yield f"data: {json.dumps({'type': 'error', 'text': state['error']}, ensure_ascii=False)}\n\n"
                elif state['result_date']:
                    yield f"data: {json.dumps({'type': 'complete', 'date': state['result_date']}, ensure_ascii=False)}\n\n"
                yield "data: {\"type\": \"done\"}\n\n"
                break

            time.sleep(0.5)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ──────────────────────────────────────────────
# 主页面（SPA）
# ──────────────────────────────────────────────
MAIN_PAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>短线助手 — 强势股筛选</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body {
            font-family: -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
            background: #f5f7fa; color:#333; line-height:1.6;
        }
        .container { max-width:1480px; margin:0 auto; padding:20px; }

        /* 顶部导航 */
        .top-bar {
            display:flex; align-items:center; justify-content:space-between;
            background: linear-gradient(135deg,#2c3e50 0%,#34495e 50%,#4a6741 100%);
            color:white; padding:20px 32px; border-radius:16px; margin-bottom:20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.15); flex-wrap:wrap; gap:16px;
        }
        .top-bar h1 { font-size:28px; font-weight:700; }
        .top-bar .right-section { display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
        .btn-run {
            background: #e74c3c; color:white; border:none;
            padding:12px 28px; border-radius:12px; font-size:16px; font-weight:700;
            cursor:pointer; transition: all 0.3s; white-space:nowrap;
        }
        .btn-run:hover:not(:disabled) { background:#c0392b; transform:translateY(-1px); box-shadow:0 4px 12px rgba(231,76,60,0.4); }
        .btn-run:disabled { background:#95a5a6; cursor:not-allowed; }
        .btn-refresh {
            background:#2563eb;
        }
        .btn-refresh:hover:not(:disabled) { background:#1d4ed8; box-shadow:0 4px 12px rgba(37,99,235,0.35); }
        .last-date { font-size:14px; opacity:0.9; }

        /* 步骤面板 */
        .steps-panel {
            background:white; border-radius:16px; padding:20px 24px; margin-bottom:20px;
            box-shadow:0 2px 12px rgba(0,0,0,0.06);
        }
        .steps-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
        .steps-header h3 { font-size:16px; color:#2c3e50; }
        .steps-status { font-size:13px; color:#888; }
        .steps-list { display:flex; gap:12px; }
        .step-item {
            flex:1; display:flex; align-items:center; gap:10px;
            padding:12px 14px; border-radius:12px;
            background:#f8f9fa; border:2px solid transparent;
            transition: all 0.3s;
        }
        .step-item.active { background:#e8f5e9; border-color:#4caf50; }
        .step-item.done { background:#e3f2fd; border-color:#2196f3; }
        .step-item.error { background:#ffebee; border-color:#f44336; }
        .step-icon {
            width:28px; height:28px; border-radius:50%; background:#bdc3c7;
            color:white; font-size:13px; font-weight:700;
            display:flex; align-items:center; justify-content:center; flex-shrink:0;
        }
        .step-item.active .step-icon { background:#4caf50; }
        .step-item.done .step-icon { background:#2196f3; }
        .step-item.error .step-icon { background:#f44336; }
        .step-info { flex:1; min-width:0; }
        .step-title { font-size:13px; font-weight:600; color:#555; }
        .step-desc { font-size:11px; color:#999; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .step-status { font-size:16px; }
        .progress-bar-outer { height:10px; background:#ecf0f1; border-radius:5px; overflow:hidden; }
        .progress-bar-inner { height:100%; background:linear-gradient(90deg,#27ae60,#2ecc71); border-radius:5px; transition:width 0.5s; width:0%; }
        .log-console {
            background:#1a1a2e; color:#00ff88; border-radius:10px; padding:12px 14px;
            font-family:'Consolas','Courier New',monospace; font-size:11px;
            max-height:140px; overflow-y:auto; white-space:pre-wrap; line-height:1.5;
            margin-top:12px;
        }
        .spinner { display:inline-block; width:14px; height:14px; border:2px solid #ddd; border-top-color:#e74c3c; border-radius:50%; animation:spin 0.8s linear infinite; margin-right:6px; }
        @keyframes spin { to { transform:rotate(360deg); } }

        /* 统计卡片 */
        .stats-bar { display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:20px; }
        .stat-card {
            background:white; border-radius:12px; padding:24px 20px; text-align:center;
            box-shadow:0 2px 12px rgba(0,0,0,0.06); transition:transform 0.2s;
        }
        .stat-card:hover { transform:translateY(-2px); }
        .stat-value { font-size:36px; font-weight:700; color:#2c3e50; margin-bottom:4px; }
        .stat-label { font-size:13px; color:#888; }
        .stat-card.best .stat-value { color:#e74c3c; }

        /* 最佳高亮 */
        .best-banner {
            background:linear-gradient(135deg,#e74c3c 0%,#c0392b 100%); color:white;
            border-radius:16px; padding:24px 36px; margin-bottom:20px;
            display:flex; align-items:center; justify-content:space-between;
            box-shadow:0 4px 20px rgba(231,76,60,0.3); flex-wrap:wrap; gap:16px;
        }
        .best-label { font-size:14px; opacity:0.9; margin-bottom:4px; }
        .best-name { font-size:26px; font-weight:700; }
        .best-code { font-size:14px; opacity:0.8; }
        .best-metrics { display:flex; gap:28px; flex-wrap:wrap; }
        .best-metric { text-align:center; }
        .best-metric-val { font-size:26px; font-weight:700; }
        .best-metric-label { font-size:12px; opacity:0.8; }

        /* 表格 */
        .section-card {
            background:white; border-radius:16px; padding:24px; margin-bottom:20px;
            box-shadow:0 2px 12px rgba(0,0,0,0.06);
        }
        .section-card h2 { font-size:18px; margin-bottom:16px; color:#2c3e50; display:flex; align-items:center; gap:8px; }
        .section-card h2::before { content:''; width:4px; height:20px; background:#e74c3c; border-radius:2px; }
        table { width:100%; border-collapse:collapse; font-size:14px; }
        th { background:#f8f9fa; padding:10px 12px; text-align:center; font-weight:600; color:#555; border-bottom:2px solid #e0e0e0; white-space:nowrap; }
        td { padding:10px 12px; text-align:center; border-bottom:1px solid #f0f0f0; }
        tr:hover td { background:#fafbfc; }
        .text-muted { color:#888; }
        .table-rank { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; border-radius:50%; font-weight:700; font-size:13px; color:white; }
        .tr-1 .table-rank { background:#e74c3c; } .tr-2 .table-rank { background:#f39c12; }
        .tr-3 .table-rank { background:#27ae60; } .tr-4 .table-rank, tr-5 .table-rank { background:#3498db; }
        .tr-rest .table-rank { background:#bdc3c7; }

        /* 股票卡片 */
        .stock-card {
            background:white; border-radius:16px; padding:20px 24px; margin-bottom:20px;
            box-shadow:0 2px 12px rgba(0,0,0,0.06);
        }
        .stock-header { display:flex; align-items:center; gap:14px; margin-bottom:16px; padding-bottom:14px; border-bottom:1px solid #f0f0f0; flex-wrap:wrap; }
        .rank-badge { color:white; font-weight:700; width:40px; height:40px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:16px; flex-shrink:0; }
        .stock-info { flex:1; }
        .stock-name { font-size:20px; font-weight:700; color:#2c3e50; }
        .stock-code { color:#888; font-size:13px; }
        .stock-tags { display:flex; gap:8px; flex-wrap:wrap; }
        .tag { padding:5px 14px; border-radius:20px; font-size:12px; font-weight:600; }
        .tag-composite { background:#e3f2fd; color:#1565c0; }
        .tag-momentum { background:#fce4ec; color:#c62828; }
        .tag-risk { background:#fff3e0; color:#ef6c00; }
        .tag-mode { background:#eef2f7; color:#546e7a; }
        .tag-change { font-weight:700; }
        .charts-row { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
        .chart-box { background:#fafbfc; border-radius:12px; padding:12px; }
        .chart-title { font-size:13px; font-weight:600; color:#666; margin-bottom:8px; padding-left:4px; }
        .no-data { display:flex; align-items:center; justify-content:center; height:320px; color:#999; font-size:16px; }

        /* 中间结果面板 */
        .step-result-panel {
            background:white; border-radius:16px; margin-bottom:20px;
            box-shadow:0 2px 12px rgba(0,0,0,0.06); overflow:hidden;
        }
        .step-result-header {
            display:flex; align-items:center; justify-content:space-between;
            padding:16px 24px; cursor:pointer; user-select:none; gap:12px;
            border-bottom:1px solid transparent; transition:background 0.2s;
        }
        .step-result-header:hover { background:#fafbfc; }
        .step-result-header .result-title { font-size:15px; font-weight:600; color:#2c3e50; }
        .step-result-header .result-count { font-size:12px; color:#888; }
        .step-result-header .arrow { font-size:14px; color:#aaa; transition:transform 0.3s; flex-shrink:0; }
        .step-result-panel.expanded .step-result-header { border-bottom-color:#f0f0f0; }
        .step-result-panel.expanded .arrow { transform:rotate(180deg); }
        .step-result-body { display:none; padding:16px 24px; }
        .step-result-panel.expanded .step-result-body { display:block; }

        /* 三列布局 */
        .triple-column { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; }
        .triple-column .col-title {
            font-size:13px; font-weight:600; color:#555; margin-bottom:8px;
            padding-bottom:6px; border-bottom:2px solid; display:flex; align-items:center; justify-content:space-between;
        }
        .col-title.vol { border-color:#e74c3c; color:#e74c3c; }
        .col-title.hot { border-color:#f39c12; color:#f39c12; }
        .col-title.inter { border-color:#27ae60; color:#27ae60; }
        .mini-list { max-height:420px; overflow-y:auto; }
        .mini-list .stock-row {
            display:flex; align-items:center; justify-content:space-between;
            padding:6px 8px; border-radius:6px; font-size:13px;
            border-bottom:1px solid #f7f7f7;
        }
        .mini-list .stock-row:hover { background:#fafbfc; }
        .mini-list .stock-code { color:#888; font-size:11px; font-family:monospace; margin-right:8px; flex-shrink:0; }
        .mini-list .stock-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .mini-list .rank-num { color:#aaa; font-size:11px; margin-right:6px; }

        /* 评分表格（紧凑） */
        .score-table { width:100%; border-collapse:collapse; font-size:13px; }
        .score-table th { background:#f0f4f8; padding:8px 10px; font-size:12px; color:#666; border-bottom:2px solid #ddd; }
        .score-table td { padding:8px 10px; border-bottom:1px solid #f0f0f0; }
        .score-table .badge { display:inline-block; width:22px; height:22px; line-height:22px; border-radius:50%; color:white; font-size:11px; font-weight:700; text-align:center; }
        .badge-1 { background:#e74c3c; } .badge-2 { background:#f39c12; } .badge-3 { background:#27ae60; }
        .score-high { color:#e74c3c; font-weight:700; }
        .score-mid { color:#f39c12; font-weight:600; }
        .score-low { color:#888; }
        .guide-box {
            background:#fbfcfe; border:1px solid #e8edf3; border-radius:12px;
            padding:14px 16px; margin-bottom:16px; font-size:13px; color:#566;
            line-height:1.7;
        }
        .guide-box strong { color:#2c3e50; }
        .guide-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 18px; margin-top:8px; }
        .guide-item { padding:8px 10px; background:#fff; border-radius:10px; border:1px solid #eef2f6; }
        .guide-item code { background:#f5f7fa; padding:1px 5px; border-radius:4px; }
        .empty-state { text-align:center; padding:60px 20px; color:#999; }
        .empty-state .icon { font-size:64px; margin-bottom:16px; }
        .empty-state p { font-size:18px; margin-bottom:8px; }

        /* 排名第一历史面板 */
        .rank1-panel {
            background:white; border-radius:16px; padding:24px 28px; margin-bottom:20px;
            box-shadow:0 2px 12px rgba(0,0,0,0.06);
        }
        .rank1-panel h2 {
            font-size:18px; margin-bottom:20px; color:#2c3e50;
            display:flex; align-items:center; gap:8px;
        }
        .rank1-panel h2::before { content:''; width:4px; height:20px; background:#e74c3c; border-radius:2px; }
        .rank1-timeline {
            display:flex; gap:12px; overflow-x:auto; padding-bottom:8px;
        }
        .rank1-item {
            flex:0 0 auto; min-width:150px;
            background:linear-gradient(135deg,#fafbfc 0%,#fff 100%);
            border:1px solid #e8e8e8; border-radius:12px; padding:16px 14px;
            text-align:center; transition: all 0.2s; cursor:default;
        }
        .rank1-item:hover { border-color:#e74c3c; box-shadow:0 2px 12px rgba(231,76,60,0.15); transform:translateY(-2px); }
        .rank1-item.today { border-color:#e74c3c; background:linear-gradient(135deg,#fff5f5 0%,#fff 100%); }
        .rank1-date { font-size:12px; color:#999; margin-bottom:6px; }
        .rank1-name { font-size:16px; font-weight:700; color:#2c3e50; margin-bottom:4px; }
        .rank1-code { font-size:12px; color:#888; margin-bottom:8px; }
        .rank1-momentum { font-size:20px; font-weight:700; color:#e74c3c; }
        .rank1-momentum-label { font-size:10px; color:#999; }
        .rank1-change { font-size:12px; margin-top:4px; }
        .rank1-change.up { color:#e74c3c; }
        .rank1-change.down { color:#27ae60; }
        .rank1-empty { text-align:center; padding:20px; color:#999; font-size:14px; }

        @media (max-width:1100px) { .stats-bar { grid-template-columns:repeat(2,1fr); } .charts-row { grid-template-columns:1fr; } .triple-column { grid-template-columns:1fr !important; } }
        @media (max-width:600px) { .stats-bar { grid-template-columns:1fr; } .guide-grid { grid-template-columns:1fr; } }
        .page-footer { text-align:center; padding:24px; color:#999; font-size:12px; }
    </style>
</head>
<body>
    <div class="container">

        <!-- 顶部 -->
        <div class="top-bar" id="topBar">
            <h1>短线助手 — 强势股筛选</h1>
            <div class="right-section">
                <span class="last-date" id="lastDateInfo">—</span>
                <button class="btn-run" id="btnRun" onclick="startRun(false)">重新运行</button>
                <button class="btn-run btn-refresh" id="btnRefreshRun" onclick="startRun(true)">刷新数据并运行</button>
            </div>
        </div>

        <!-- 步骤面板（内联，常驻显示） -->
        <div class="steps-panel" id="stepsPanel">
            <div class="steps-header">
                <h3>分析进度</h3>
                <span class="steps-status" id="stepsStatus">就绪</span>
            </div>
            <div class="steps-list">
                <div class="step-item" id="step1">
                    <div class="step-icon">1</div>
                    <div class="step-info">
                        <div class="step-title">数据获取</div>
                        <div class="step-desc" id="step1Desc">等待开始...</div>
                    </div>
                    <div class="step-status" id="step1Status">⏳</div>
                </div>
                <div class="step-item" id="step2">
                    <div class="step-icon">2</div>
                    <div class="step-info">
                        <div class="step-title">筛选评分</div>
                        <div class="step-desc" id="step2Desc">等待...</div>
                    </div>
                    <div class="step-status" id="step2Status">⏳</div>
                </div>
                <div class="step-item" id="step3">
                    <div class="step-icon">3</div>
                    <div class="step-info">
                        <div class="step-title">图表生成</div>
                        <div class="step-desc" id="step3Desc">等待...</div>
                    </div>
                    <div class="step-status" id="step3Status">⏳</div>
                </div>
                <div class="step-item" id="step4">
                    <div class="step-icon">4</div>
                    <div class="step-info">
                        <div class="step-title">完成</div>
                        <div class="step-desc" id="step4Desc">等待...</div>
                    </div>
                    <div class="step-status" id="step4Status">⏳</div>
                </div>
            </div>
            <div class="progress-bar-outer" style="margin-top:12px;"><div class="progress-bar-inner" id="progressBar"></div></div>
            <div class="log-console" id="logConsole"></div>
        </div>

        <!-- 统计数据面板已移到步骤面板下方 -->

        <!-- 步骤1结果面板 -->
        <div class="step-result-panel" id="step1Results" style="display:none;">
            <div class="step-result-header" onclick="togglePanel('step1Results')">
                <div><span class="result-title">数据获取结果</span><span class="result-count" id="step1Count"></span></div>
                <span class="arrow">▼</span>
            </div>
            <div class="step-result-body">
                <div class="triple-column">
                    <div>
                        <div class="col-title vol">成交额榜 <span id="volCount"></span></div>
                        <div class="mini-list" id="volList"></div>
                    </div>
                    <div>
                        <div class="col-title hot">热度榜 <span id="hotCount"></span></div>
                        <div class="mini-list" id="hotList"></div>
                    </div>
                    <div>
                        <div class="col-title inter">交集 <span id="interCount"></span></div>
                        <div class="mini-list" id="interList"></div>
                    </div>
                </div>
            </div>
        </div>

        <!-- 步骤2结果面板 -->
        <div class="step-result-panel" id="step2Results" style="display:none;">
            <div class="step-result-header" onclick="togglePanel('step2Results')">
                <div><span class="result-title">筛选评分结果</span><span class="result-count" id="step2Count"></span></div>
                <span class="arrow">▼</span>
            </div>
            <div class="step-result-body">
                <div class="guide-box">
                    <strong>筛选评分结果怎么看：</strong>
                    这里展示的是本次运行的候选池评分过程，先由成交额榜和热度榜形成交集或软合并，再加入动量、风险、板块强度后排序；最终进入下方“TOP强势股汇总”的，是这里排名靠前的股票。
                </div>
                <div style="overflow-x:auto;">
                    <table class="score-table">
                        <thead><tr>
                            <th>排名</th><th>代码</th><th>名称</th><th>最终评分</th><th>资金人气</th><th>风险调整动量</th><th>板块</th><th>板块强度</th><th>板块来源</th><th>回撤%</th><th>过热惩罚</th><th>入选方式</th>
                        </tr></thead>
                        <tbody id="scoreBody"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 统计卡片 -->
        <div class="stats-bar" id="statsBar"></div>

        <!-- 最佳高亮 -->
        <div id="bestBanner"></div>

        <!-- 近7天排名第一 -->
        <div class="rank1-panel" id="rank1Panel">
            <h2>近7日每日最佳综合评分股列表</h2>
            <div class="rank1-timeline" id="rank1Timeline">
                <div class="rank1-empty">暂无历史记录，运行一次分析后开始积累</div>
            </div>
        </div>

        <!-- 汇总表格 -->
        <div class="section-card" id="summarySection">
            <h2>TOP强势股汇总</h2>
            <div class="guide-box">
                <strong>两个板块的区别：</strong>
                <div>“筛选评分结果”是步骤 2 的中间结果，重点看候选股如何从交集池里被打分和排序；“TOP强势股汇总”是最终输出，给你最值得看的前 N 只，并联动下面的个股图表。</div>
                <div class="guide-grid">
                    <div class="guide-item"><strong>最终评分</strong>：综合分，越高越好。</div>
                    <div class="guide-item"><strong>资金人气</strong>：成交额与热度的合成，越高说明资金和关注度越集中。</div>
                    <div class="guide-item"><strong>风险调整动量</strong>：原始动量扣掉波动和回撤后的结果，适合看“稳不稳”。</div>
                    <div class="guide-item"><strong>板块强度</strong>：来自财联社热门板块或本地规则，越高说明板块越热。</div>
                    <div class="guide-item"><strong>回撤%</strong>：近段时间从高点回落的幅度，越小越好。</div>
                    <div class="guide-item"><strong>过热惩罚</strong>：短期涨太快、冲得太急时会升高，越小越健康。</div>
                </div>
            </div>
            <div style="overflow-x:auto;"><table><thead id="tableHead"></thead><tbody id="tableBody"></tbody></table></div>
        </div>

        <!-- 个股详情 -->
        <div class="section-card">
            <h2>个股K线 & 动量分析</h2>
        </div>
        <div id="stockCards"></div>

        <div class="page-footer">
            <p>短线助手 shortais | 存储: SQLite | 数据来源: dcsdk / pywencai / akshare / 财联社热门板块 | 仅供参考，不构成投资建议</p>
        </div>
    </div>

    <script>
    var KLINE_DAYS = 60;
    var MOMENTUM_DAYS = 25;

    // ── 加载上次结果 ──
    async function loadLastRun() {
        try {
            var resp = await fetch('/api/last-run');
            var data = await resp.json();
            if (data.has_data) {
                document.getElementById('lastDateInfo').textContent = '上次运行: ' + data.date;
                await loadResults(data.date);
            } else {
                document.getElementById('lastDateInfo').textContent = '暂无历史数据';
                showEmpty();
                // 无结果时只加载历史记录
                loadRank1History();
            }
        } catch(e) {
            document.getElementById('lastDateInfo').textContent = '加载失败';
            showEmpty();
        }
    }

    // ── 加载结果 ──
    async function loadResults(dateStr) {
        try {
            // 显示loading
            document.getElementById('stockCards').innerHTML = '<div class="empty-state"><p>正在加载图表数据...</p></div>';
            var resp = await fetch('/api/results/' + dateStr);
            if (!resp.ok) { showEmpty(); return; }
            var data = await resp.json();
            KLINE_DAYS = data.kline_days || 60;
            MOMENTUM_DAYS = data.momenta_days || 25;
            renderResults(data);
        } catch(e) {
            showEmpty();
        }
    }

    // ── 渲染结果 ──
    function renderResults(data) {
        var stocks = data.stocks;
        var stats = data.stats;

        // 隐藏loading
        document.getElementById('lastDateInfo').textContent = '上次运行: ' + data.date;
        document.getElementById('topBar').querySelector('h1').textContent = '短线助手 — 强势股筛选报告 (' + data.date + ')';

        // 统计卡片
        document.getElementById('statsBar').innerHTML = [
            '<div class="stat-card"><div class="stat-value">' + stats.total + '</div><div class="stat-label">总股票数</div></div>',
            '<div class="stat-card"><div class="stat-value">' + stats.total + '</div><div class="stat-label">成功分析</div></div>',
            '<div class="stat-card"><div class="stat-value">' + (stats.avg_final || stats.avg_momentum).toFixed(2) + '</div><div class="stat-label">平均最终评分</div></div>',
            '<div class="stat-card best"><div class="stat-value">' + stats.best_stock.name + '</div><div class="stat-label">最佳综合评分股</div></div>'
        ].join('');

        // 最佳高亮
        var bs = stats.best_stock;
        var pcSign = bs.period_change >= 0 ? '+' : '';
        document.getElementById('bestBanner').innerHTML = [
            '<div class="best-banner">',
            '<div>',
            '<div class="best-label">最佳综合评分股</div>',
            '<div class="best-name">' + bs.name + '</div>',
            '<div class="best-code">' + bs.code + '</div>',
            '</div>',
            '<div class="best-metrics">',
            '<div class="best-metric"><div class="best-metric-val">#' + bs.rank + '</div><div class="best-metric-label">最终排名</div></div>',
            '<div class="best-metric"><div class="best-metric-val">' + (bs.final_score || bs.momentum).toFixed(2) + '</div><div class="best-metric-label">最终评分</div></div>',
            '<div class="best-metric"><div class="best-metric-val">' + bs.momentum.toFixed(2) + '</div><div class="best-metric-label">原始动量</div></div>',
            '<div class="best-metric"><div class="best-metric-val">' + pcSign + bs.period_change.toFixed(2) + '%</div><div class="best-metric-label">期间涨跌</div></div>',
            '</div></div>'
        ].join('');

        // 今日最佳插入到近7日面板最前面
        var todayStr = new Date().toISOString().slice(0,10).replace(/-/g,'');
        var todayCard = [
            '<div class="rank1-item today">',
            '<div class="rank1-date">今日</div>',
            '<div class="rank1-name">' + bs.name + '</div>',
            '<div class="rank1-code">' + bs.code + '</div>',
            '<div class="rank1-momentum">' + (bs.final_score || bs.momentum).toFixed(2) + '</div>',
            '<div class="rank1-momentum-label">最终评分</div>',
            '<div class="rank1-change ' + (bs.period_change >= 0 ? 'up' : 'down') + '">' + pcSign + bs.period_change.toFixed(2) + '%</div>',
            '</div>'
        ].join('');
        document.getElementById('rank1Timeline').innerHTML = todayCard;

        // 表格
        var tableHeads = '<tr><th>排名</th><th>名称</th><th>代码</th><th>最终评分</th><th>资金人气</th><th>原始动量</th><th>风险调整动量</th><th>板块</th><th>板块强度</th><th>板块来源</th><th>回撤%</th><th>过热惩罚</th><th>入选方式</th><th>期间涨跌</th></tr>';
        document.getElementById('tableHead').innerHTML = tableHeads;
        var tbody = '';
        stocks.forEach(function(s) {
            var changeColor = s.period_change >= 0 ? '#e74c3c' : '#2ecc71';
            var pcStr = (s.period_change >= 0 ? '+' : '') + s.period_change.toFixed(2) + '%';
            var trClass = s.rank <= 3 ? 'tr-' + s.rank : 'tr-rest';
            tbody += [
                '<tr class="' + trClass + '" onclick="scrollToCard(\'' + s.code + '\')" style="cursor:pointer;">',
                '<td><span class="table-rank">' + s.rank + '</span></td>',
                '<td><strong>' + s.name + '</strong></td>',
                '<td class="text-muted">' + s.code + '</td>',
                '<td><strong>' + (s.final_score || s.momentum).toFixed(2) + '</strong></td>',
                '<td>' + s.composite.toFixed(4) + '</td>',
                '<td>' + s.momentum.toFixed(2) + '</td>',
                '<td>' + (s.risk_adj_momentum || s.momentum).toFixed(2) + '</td>',
                '<td>' + (s.sector_name || '未知') + '</td>',
                '<td>' + (s.sector_strength || 0.5).toFixed(4) + '</td>',
                '<td>' + (s.sector_source || 'fallback') + '</td>',
                '<td>' + (s.max_drawdown || 0).toFixed(2) + '</td>',
                '<td>' + (s.overheat_penalty || 0).toFixed(4) + '</td>',
                '<td>' + (s.candidate_mode === 'intersection' ? '交集' : '软合并') + '</td>',
                '<td style="color:' + changeColor + ';font-weight:700;">' + pcStr + '</td>',
                '</tr>'
            ].join('');
        });
        document.getElementById('tableBody').innerHTML = tbody;

        // 股票卡片
        var cards = '';
        var rankColors = ['#e74c3c','#f39c12','#27ae60','#3498db','#3498db','#3498db','#3498db','#3498db','#3498db','#3498db'];
        // 使用基于 code 的稳定 ID，避免 Date.now() 导致 setTimeout 中找不到元素
        var renderToken = Date.now();
        stocks.forEach(function(s, idx) {
            var pcSign2 = s.period_change >= 0 ? '+' : '';
            var pcColor2 = s.period_change >= 0 ? '#e74c3c' : '#2ecc71';
            var cardId = 'card-' + s.code + '-' + renderToken;
            cards += [
                '<div class="stock-card" id="' + cardId + '">',
                '<div class="stock-header">',
                '<div class="rank-badge" style="background:' + rankColors[idx] + '">#' + s.rank + '</div>',
                '<div class="stock-info"><div class="stock-name">' + s.name + '</div><div class="stock-code">' + s.code + '</div></div>',
                '<div class="stock-tags">',
                '<span class="tag tag-composite">最终 ' + (s.final_score || s.momentum).toFixed(2) + '</span>',
                '<span class="tag tag-momentum">动量 ' + s.momentum.toFixed(2) + '</span>',
                '<span class="tag tag-risk">回撤 ' + (s.max_drawdown || 0).toFixed(2) + '%</span>',
                '<span class="tag tag-mode">' + (s.candidate_mode === 'intersection' ? '交集' : '软合并') + '</span>',
                '<span class="tag tag-mode">板块 ' + (s.sector_name || '未知') + '</span>',
                '<span class="tag tag-change" style="background:' + pcColor2 + '20;color:' + pcColor2 + '">期间 ' + pcSign2 + s.period_change.toFixed(2) + '%</span>',
                '</div></div>',
                '<div class="charts-row">',
                '<div class="chart-box"><div class="chart-title">K线图（过去' + KLINE_DAYS + '日）</div><div id="kline-' + cardId + '" style="width:100%;height:320px;"></div></div>',
                '<div class="chart-box"><div class="chart-title">动量分析（过去' + MOMENTUM_DAYS + '日）</div><div id="mom-' + cardId + '" style="width:100%;height:320px;"></div></div>',
                '</div></div>'
            ].join('');
        });
        document.getElementById('stockCards').innerHTML = cards;

        // 初始化图表（使用与渲染时相同的 cardId）
        setTimeout(function() {
            stocks.forEach(function(s, idx) {
                var cardId = 'card-' + s.code + '-' + renderToken;
                initKlineChart('kline-' + cardId, s.kline_data);
                initMomentumChart('mom-' + cardId, s.momentum_data);
            });
        }, 200);

        // 加载历史记录追加到今日卡片后面
        loadRank1History();
    }

    // ── K线图 ──
    function initKlineChart(domId, data) {
        var el = document.getElementById(domId);
        if (!el || !data || !data.dates || !data.dates.length) {
            if (el) el.innerHTML = '<div class="no-data">K线数据不足</div>';
            return;
        }
        var chart = echarts.init(el);
        chart.setOption({
            tooltip: {
                trigger:'axis', axisPointer:{type:'cross'},
                formatter:function(p){
                    var k = data.kline[p[0].dataIndex];
                    return data.dates[p[0].dataIndex] + '<br/>开:' + k[0] + ' 收:' + k[1] + '<br/>低:' + k[2] + ' 高:' + k[3];
                }
            },
            grid:[{left:'8%',right:'4%',top:'10%',height:'55%'},{left:'8%',right:'4%',top:'72%',height:'20%'}],
            xAxis:[
                {type:'category',data:data.dates,scale:true,boundaryGap:false,axisLabel:{show:false}},
                {type:'category',gridIndex:1,data:data.dates,scale:true,boundaryGap:false,axisLabel:{show:true,rotate:45,fontSize:10}}
            ],
            yAxis:[
                {scale:true,splitLine:{show:true,lineStyle:{color:'#eee'}}},
                {scale:true,gridIndex:1,splitNumber:2,axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}}
            ],
            dataZoom:[{type:'inside',xAxisIndex:[0,1],start:0,end:100}],
            series:[
                {name:'K线',type:'candlestick',data:data.kline,itemStyle:{color:'#ef5350',color0:'#26a69a',borderColor:'#ef5350',borderColor0:'#26a69a'}},
                {name:'MA5',type:'line',data:data.ma5,smooth:true,lineStyle:{color:'#ffb74d',width:1.5},showSymbol:false},
                {name:'MA20',type:'line',data:data.ma20,smooth:true,lineStyle:{color:'#42a5f5',width:1.5},showSymbol:false},
                {name:'MA60',type:'line',data:data.ma60,smooth:true,lineStyle:{color:'#ce93d8',width:1.5},showSymbol:false},
                {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,
                 data: data.volumes.map(function(v){return v[1];}),
                 itemStyle:{color:function(p){return data.volumes[p.dataIndex][2]>0?'#ef5350':'#26a69a';}}}
            ]
        });
        window.addEventListener('resize',function(){chart.resize();});
    }

    // ── 动量图 ──
    function initMomentumChart(domId, data) {
        var el = document.getElementById(domId);
        if (!el || !data || !data.days || !data.days.length) {
            if (el) el.innerHTML = '<div class="no-data">动量数据不足</div>';
            return;
        }
        var colors = data.prices.map(function(p,i){
            return i===0?'#91cc75':(p>=data.prices[i-1]?'#ef5350':'#26a69a');
        });
        var chart = echarts.init(el);
        chart.setOption({
            tooltip:{trigger:'axis'},
            grid:[{left:'8%',right:'4%',top:'10%',height:'55%'},{left:'8%',right:'4%',top:'72%',height:'20%'}],
            xAxis:[
                {type:'category',data:data.days,axisLabel:{show:false}},
                {type:'category',gridIndex:1,data:data.days,axisLabel:{show:true,fontSize:10}}
            ],
            yAxis:[
                {type:'value',name:'相对价格',splitLine:{show:true,lineStyle:{color:'#eee'}}},
                {type:'value',gridIndex:1,name:'收盘价',splitLine:{show:false}}
            ],
            series:[
                {name:'相对价格',type:'line',data:data.relative,lineStyle:{color:'#5470c6',width:2},symbol:'circle',symbolSize:4},
                {name:'趋势线',type:'line',data:data.trend_line,lineStyle:{color:'#ee6666',width:2,type:'dashed'},showSymbol:false},
                {name:'收盘价',type:'bar',xAxisIndex:1,yAxisIndex:1,data:data.prices,itemStyle:{color:function(p){return colors[p.dataIndex];}}}
            ]
        });
        window.addEventListener('resize',function(){chart.resize();});
    }

    // ── 空状态 ──
    function showEmpty() {
        document.getElementById('statsBar').innerHTML = '';
        document.getElementById('bestBanner').innerHTML = '';
        document.getElementById('tableHead').innerHTML = '';
        document.getElementById('tableBody').innerHTML = '';
        document.getElementById('stockCards').innerHTML = '<div class="empty-state"><div class="icon"> </div><p>暂无数据</p><p style="font-size:14px;">点击"重新运行"开始分析</p></div>';
    }

    // ── 近7日每日最佳动量股历史 ──
    async function loadRank1History() {
        try {
            var resp = await fetch('/api/rank1-history');
            var data = await resp.json();
            var records = data.records || [];
            var timeline = document.getElementById('rank1Timeline');
            var todayStr = new Date().toISOString().slice(0,10).replace(/-/g,'');

            if (records.length === 0 && timeline.innerHTML === '') {
                timeline.innerHTML = '<div class="rank1-empty">暂无历史记录，运行一次分析后开始积累</div>';
                return;
            }

            // 如果面板里已有"今日"卡片，保留它，只追加历史记录
            var existingHtml = timeline.innerHTML || '';
            var hasToday = existingHtml.indexOf('今日') !== -1;

            var historyHtml = '';
            records.forEach(function(r) {
                // 跳过今天的重复记录（已由 renderResults 插入）
                if (r.date === todayStr && hasToday) return;
                var pc = r.period_change || 0;
                var pcStr = (pc >= 0 ? '+' : '') + pc.toFixed(2) + '%';
                var pcClass = pc >= 0 ? 'up' : 'down';
                historyHtml += [
                    '<div class="rank1-item">',
                    '<div class="rank1-date">' + (r.date_display || r.date) + '</div>',
                    '<div class="rank1-name">' + (r.name || '') + '</div>',
                    '<div class="rank1-code">' + (r.code || '') + '</div>',
                    '<div class="rank1-momentum">' + ((r.final_score || r.momentum_score || 0).toFixed(2)) + '</div>',
                    '<div class="rank1-momentum-label">最终评分</div>',
                    '<div class="rank1-change ' + pcClass + '">' + pcStr + '</div>',
                    '</div>'
                ].join('');
            });

            if (historyHtml) {
                timeline.innerHTML = existingHtml + historyHtml;
            }
        } catch(e) {
            console.error('加载历史记录失败:', e);
        }
    }

    // ── 步骤面板更新 ──
    function togglePanel(id) {
        var panel = document.getElementById(id);
        if (panel.style.display === 'none') return;
        panel.classList.toggle('expanded');
    }

    function scrollToCard(code) {
        var card = document.querySelector('.stock-card[id*="card-' + code + '-"]');
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.style.transition = 'box-shadow 0.3s';
            card.style.boxShadow = '0 0 0 3px #3498db';
            setTimeout(function() { card.style.boxShadow = ''; }, 1500);
        }
    }

    function updateStepPanel(progress, stage, logText, isError) {
        var stepsStatus = document.getElementById('stepsStatus');
        var bar = document.getElementById('progressBar');
        var logConsole = document.getElementById('logConsole');

        // 更新总进度条
        var pct = Math.round((progress / 4) * 100);
        bar.style.width = pct + '%';

        // 更新步骤状态
        for (var i = 1; i <= 4; i++) {
            var item = document.getElementById('step' + i);
            var status = document.getElementById('step' + i + 'Status');
            var desc = document.getElementById('step' + i + 'Desc');
            item.classList.remove('active', 'done', 'error');

            if (i < progress + 1) {
                item.classList.add('done');
                status.textContent = '✓';
            } else if (i === progress + 1) {
                if (isError) {
                    item.classList.add('error');
                    status.textContent = '✗';
                } else {
                    item.classList.add('active');
                    status.textContent = '▶';
                }
            } else {
                status.textContent = '⏳';
            }
        }

        // 更新描述
        if (stage === '数据获取') {
            document.getElementById('step1Desc').textContent = logText || '获取中...';
        } else if (stage === '筛选评分') {
            document.getElementById('step1Desc').textContent = '已完成';
            document.getElementById('step2Desc').textContent = logText || '分析中...';
        } else if (stage === '图表生成') {
            document.getElementById('step1Desc').textContent = '已完成';
            document.getElementById('step2Desc').textContent = '已完成';
            document.getElementById('step3Desc').textContent = logText || '生成中...';
        } else if (stage === '完成') {
            document.getElementById('step1Desc').textContent = '已完成';
            document.getElementById('step2Desc').textContent = '已完成';
            document.getElementById('step3Desc').textContent = '已完成';
            document.getElementById('step4Desc').textContent = logText || '分析完成';
        } else if (stage === '错误') {
            stepsStatus.textContent = '运行失败';
            stepsStatus.style.color = '#f44336';
        }

        if (stage === '完成') {
            stepsStatus.textContent = '已完成';
            stepsStatus.style.color = '#4caf50';
        } else if (!isError && stage !== '错误') {
            stepsStatus.textContent = stage + '...';
            stepsStatus.style.color = '#2196f3';
        }
    }

    // ── 渲染步骤1数据 ──
    function renderStep1Data(data) {
        var panel = document.getElementById('step1Results');
        panel.style.display = 'block';
        panel.classList.add('expanded');

        document.getElementById('step1Count').textContent =
            '（成交量' + data.volume_count + ' + 热度' + data.hot_count + ' → 交集' + data.intersection_count + '）';

        // 成交量榜
        document.getElementById('volCount').textContent = '(共' + data.volume_count + '只)';
        var volHtml = '';
        (data.volume_stocks || []).forEach(function(s, i) {
            volHtml += '<div class="stock-row"><span class="rank-num">' + (i+1) + '</span><span class="stock-code">' + (s.code||'') + '</span><span class="stock-name">' + (s.name||'') + '</span></div>';
        });
        document.getElementById('volList').innerHTML = volHtml;

        // 热度榜
        document.getElementById('hotCount').textContent = '(共' + data.hot_count + '只)';
        var hotHtml = '';
        (data.hot_stocks || []).forEach(function(s, i) {
            hotHtml += '<div class="stock-row"><span class="rank-num">' + (i+1) + '</span><span class="stock-code">' + (s.code||'') + '</span><span class="stock-name">' + (s.name||'') + '</span></div>';
        });
        document.getElementById('hotList').innerHTML = hotHtml;

        // 交集
        document.getElementById('interCount').textContent = '(共' + data.intersection_count + '只)';
        var interHtml = '';
        (data.intersection_stocks || []).forEach(function(s, i) {
            interHtml += '<div class="stock-row"><span class="rank-num">' + (i+1) + '</span><span class="stock-code">' + (s.code||'') + '</span><span class="stock-name">' + (s.name||'') + '</span></div>';
        });
        document.getElementById('interList').innerHTML = interHtml;
    }

    // ── 渲染步骤2数据 ──
    function renderStep2Data(data) {
        var panel = document.getElementById('step2Results');
        panel.style.display = 'block';
        panel.classList.add('expanded');

        document.getElementById('step2Count').textContent =
            '（交集' + data.intersection_count + '只 → 风险调整综合评分 → TOP' + data.top_count + '）';

        var tbody = '';
        var badgeClass = ['', 'badge-1', 'badge-2', 'badge-3'];
        (data.scored_stocks || []).forEach(function(s, i) {
            var rank = i + 1;
            var bc = badgeClass[rank] || '';
            var finalScore = s.final_score || s.momentum_score || 0;
            var mclass = finalScore >= 70 ? 'score-high' : (finalScore >= 40 ? 'score-mid' : 'score-low');
            tbody += '<tr>' +
                '<td><span class="badge ' + bc + '">' + rank + '</span></td>' +
                '<td style="font-family:monospace;color:#888;">' + (s.code||'') + '</td>' +
                '<td><strong>' + (s.name||'') + '</strong></td>' +
                '<td class="' + mclass + '">' + finalScore.toFixed(2) + '</td>' +
                '<td>' + (s.composite_score||0).toFixed(4) + '</td>' +
                '<td>' + (s.risk_adj_momentum || s.momentum_score || 0).toFixed(2) + '</td>' +
                '<td>' + (s.sector_name || '未知') + '</td>' +
                '<td>' + (s.sector_strength || 0.5).toFixed(4) + '</td>' +
                '<td>' + (s.sector_source || 'fallback') + '</td>' +
                '<td>' + (s.max_drawdown || 0).toFixed(2) + '</td>' +
                '<td>' + (s.overheat_penalty || 0).toFixed(4) + '</td>' +
                '<td>' + (s.candidate_mode === 'intersection' ? '交集' : '软合并') + '</td>' +
                '</tr>';
        });
        document.getElementById('scoreBody').innerHTML = tbody;
    }

    // ── 重新运行 ──
    async function startRun(forceRefresh) {
        var btn = document.getElementById('btnRun');
        var refreshBtn = document.getElementById('btnRefreshRun');
        btn.disabled = true;
        refreshBtn.disabled = true;
        btn.textContent = forceRefresh ? '等待刷新...' : '运行中...';
        refreshBtn.textContent = forceRefresh ? '刷新中...' : '刷新数据并运行';

        // 重置步骤面板
        var logConsole = document.getElementById('logConsole');
        logConsole.textContent = '';
        document.getElementById('progressBar').style.width = '0%';
        for (var i = 1; i <= 4; i++) {
            document.getElementById('step' + i).classList.remove('active', 'done', 'error');
            document.getElementById('step' + i + 'Status').textContent = '⏳';
            document.getElementById('step' + i + 'Desc').textContent = i === 1 ? '等待开始...' : '等待...';
        }
        document.getElementById('stepsStatus').textContent = '准备中...';
        document.getElementById('stepsStatus').style.color = '#888';

        // 隐藏/清空中间结果面板
        document.getElementById('step1Results').style.display = 'none';
        document.getElementById('step1Results').classList.remove('expanded');
        document.getElementById('step2Results').style.display = 'none';
        document.getElementById('step2Results').classList.remove('expanded');
        document.getElementById('volList').innerHTML = '';
        document.getElementById('hotList').innerHTML = '';
        document.getElementById('interList').innerHTML = '';
        document.getElementById('scoreBody').innerHTML = '';

        // 清空主结果区域（评分表 + 股票卡片 + 统计条 + Banner）
        document.getElementById('tableHead').innerHTML = '';
        document.getElementById('tableBody').innerHTML = '';
        document.getElementById('stockCards').innerHTML = '';
        document.getElementById('statsBar').innerHTML = '';
        document.getElementById('bestBanner').innerHTML = '';

        try {
            var resp = await fetch('/api/run', {
                method:'POST',
                headers:{'Content-Type':'application/json'},
                body: JSON.stringify({force_refresh: !!forceRefresh})
            });
            if (!resp.ok) {
                var err = await resp.json();
                alert('启动失败: ' + (err.error||'未知错误'));
                resetBtn();
                return;
            }
        } catch(e) {
            alert('请求失败: ' + e.message);
            resetBtn();
            return;
        }

        // 连接 SSE
        var eventSource = new EventSource('/api/status/stream');
        var finalDate = null;

        eventSource.onmessage = function(event) {
            var data = JSON.parse(event.data);

            if (data.type === 'log') {
                logConsole.textContent += data.text + '\n';
                logConsole.scrollTop = logConsole.scrollHeight;
            }

            if (data.type === 'progress') {
                updateStepPanel(data.progress, data.stage, '');
            }

            if (data.type === 'step_data') {
                if (data.step === 'step1') renderStep1Data(data.data);
                if (data.step === 'step2') renderStep2Data(data.data);
            }

            if (data.type === 'error') {
                updateStepPanel(data.progress, '错误', data.text, true);
                setTimeout(function(){ resetBtn(); }, 3000);
                eventSource.close();
            }

            if (data.type === 'complete') {
                finalDate = data.date;
            }

            if (data.type === 'done') {
                updateStepPanel(4, '完成', '分析完成');
                eventSource.close();
                if (finalDate) {
                    setTimeout(async function() {
                        await loadResults(finalDate);
                        resetBtn();
                    }, 800);
                } else {
                    setTimeout(function(){ resetBtn(); }, 1500);
                }
            }
        };

        eventSource.onerror = function() {
            eventSource.close();
            if (!finalDate) {
                fetch('/api/status').then(function(r){return r.json();}).then(function(s){
                    if (!s.running && s.result_date) {
                        loadResults(s.result_date);
                    }
                    resetBtn();
                });
            }
        };
    }

    function resetBtn() {
        var btn = document.getElementById('btnRun');
        var refreshBtn = document.getElementById('btnRefreshRun');
        btn.disabled = false;
        refreshBtn.disabled = false;
        btn.textContent = '重新运行';
        refreshBtn.textContent = '刷新数据并运行';
    }

    // ── 初始化 ──
    loadLastRun();
    </script>
</body>
</html>'''


@app.route('/')
def index():
    return MAIN_PAGE


# ──────────────────────────────────────────────
# 启动入口
# ──────────────────────────────────────────────
def main():
    host = '127.0.0.1'
    port = 5678

    # 初始化数据库
    db.init_db()

    print(f"""
{'='*55}
   短线助手 — 强势股筛选系统
   Web 服务: http://{host}:{port}
   存储引擎: SQLite (data/shortais.db)
   按 Ctrl+C 停止服务
{'='*55}
""")

    # 自动打开浏览器
    import webbrowser
    _ = webbrowser.open(f'http://{host}:{port}')

    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == '__main__':
    main()

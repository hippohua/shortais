"""
短线助手 — 可视化报告生成模块
读取筛选结果，生成一屏统览的 HTML 网页报告
输出: data/{date}/report.html
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import time
import json
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

# 扶摇API
FUYAO_AVAILABLE = False
try:
    from config import FUYAO_API_KEY
    if FUYAO_API_KEY and FUYAO_API_KEY.strip():
        from fuyao_client import fetch_kline as fuyao_kline
        FUYAO_AVAILABLE = True
except Exception:
    pass

from config import (
    MOMENTUM_DAYS, MOMENTUM_TOP_N, KLINE_DAYS, OUTPUT_DIR
)

# ---- mootdx 客户端（单例，带重连验证） ----
_mootdx_client = None
_mootdx_init_ok = False
_MOOTDX_RETRIES = 3
_AK_RETRIES = 2
_RETRY_BACKOFF = 1.5


def _mootdx_to_symbol(code: str) -> str:
    """6位代码 → mootdx 格式（market='std' 只需6位数字，不要加sh/sz前缀）"""
    return str(code).zfill(6)


def _get_mootdx_client():
    """获取 mootdx 行情客户端（带连接验证 + 自动重连 + 超时保护）"""
    global _mootdx_client, _mootdx_init_ok
    if _mootdx_client is not None:
        return _mootdx_client
    if _mootdx_init_ok is False and MOOTDX_AVAILABLE:
        for attempt in range(_MOOTDX_RETRIES):
            try:
                _mootdx_client = Quotes.factory(market='std', timeout=8)
                # 连接验证带超时保护
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_mootdx_client.bars, symbol='600519', frequency=4, offset=1)
                    try:
                        test = future.result(timeout=12)
                    except concurrent.futures.TimeoutError:
                        print(f"  [WARN] mootdx 连接验证超时（尝试{attempt+1}/{_MOOTDX_RETRIES}）")
                        _mootdx_client = None
                        if attempt < _MOOTDX_RETRIES - 1:
                            time.sleep(_RETRY_BACKOFF ** attempt)
                        continue
                if test is not None and not test.empty:
                    _mootdx_init_ok = True
                    return _mootdx_client
            except Exception:
                _mootdx_client = None
                if attempt < _MOOTDX_RETRIES - 1:
                    time.sleep(_RETRY_BACKOFF ** attempt)
        _mootdx_init_ok = False
    return _mootdx_client


def _normalize_df(df) -> pd.DataFrame:
    """mootdx → 中文列名"""
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


def fetch_kline_data(code: str, days: int, end_date: str) -> pd.DataFrame:
    """
    获取个股K线数据用于画图
    路径: 扶摇API → dcsdk → mootdx(try 3次, 双frequency) → akshare(try 2次) → 空
    """
    import time as _time

    # === 路径0: 同花顺扶摇API（首选） ===
    if FUYAO_AVAILABLE:
        try:
            df = fuyao_kline(code, days=days, end_date=end_date, adjust='forward')
            if df is not None and not df.empty:
                return df.tail(days)
        except Exception as e:
            print(f"    [WARN] 扶摇API K线 {code} 获取失败: {e}")

    # === 路径1: dcsdk ===
    if DCSDK_AVAILABLE:
        try:
            raw = dcsdk_kline(str(code), "day")
            if raw:
                df = pd.DataFrame(raw)
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

    # === 路径2: mootdx ===
    if MOOTDX_AVAILABLE:
        client = _get_mootdx_client()
        if client is not None:
            symbol = _mootdx_to_symbol(code)
            for attempt in range(_MOOTDX_RETRIES):
                for freq in [9, 4]:
                    try:
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                            future = executor.submit(client.bars, symbol=symbol, frequency=freq, offset=days)
                            try:
                                df = future.result(timeout=15)
                            except concurrent.futures.TimeoutError:
                                continue
                        if df is not None and not df.empty:
                            return _normalize_df(df).tail(days)
                    except Exception:
                        pass
                if attempt < _MOOTDX_RETRIES - 1:
                    _time.sleep(_RETRY_BACKOFF ** attempt)
                    global _mootdx_client
                    _mootdx_client = None
                    client = _get_mootdx_client()
                    if client is None:
                        break

    # === 路径3: akshare（兜底） ===
    if AKSHARE_AVAILABLE:
        end_dt = datetime.strptime(end_date, '%Y%m%d')
        start_dt = end_dt - timedelta(days=days * 3)
        for attempt in range(_AK_RETRIES):
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
                if attempt < _AK_RETRIES - 1:
                    _time.sleep(3 * (attempt + 1))
                    continue
                print(f"    [WARN] {code} K线数据获取失败: {e}")

    return pd.DataFrame()


def compute_momentum_for_display(prices: pd.Series) -> dict:
    """计算动量指标并返回画图所需数据"""
    prices = prices.dropna().values
    n = len(prices)
    if n < 2:
        return {
            'days': [0], 'prices': [1], 'relative': [1],
            'trend_line': [1], 'momentum': 0, 'slope': 0, 'r2': 0
        }
    relative = prices / prices[0]
    x = np.arange(n).reshape(-1, 1)

    lr = LinearRegression()
    lr.fit(x, relative)
    slope = lr.coef_[0]
    r2 = lr.score(x, relative)
    momentum = 10000 * slope * r2
    trend_line = lr.predict(x)

    return {
        'days': list(range(n)),
        'prices': prices.tolist(),
        'relative': relative.tolist(),
        'trend_line': trend_line.tolist(),
        'momentum': momentum,
        'slope': slope,
        'r2': r2,
    }


def build_kline_echarts_data(kline_df: pd.DataFrame) -> dict:
    """将K线数据转换为 ECharts 需要的格式"""
    df = kline_df.copy()
    col_map = {
        '日期': 'date', '开盘': 'open', '收盘': 'close',
        '最高': 'high', '最低': 'low', '成交量': 'volume'
    }
    df.rename(columns=col_map, inplace=True)

    required = ['date', 'open', 'high', 'low', 'close']
    if not all(c in df.columns for c in required):
        return {}

    # 计算均线
    ma5 = df['close'].rolling(window=5).mean().bfill().tolist()
    ma20 = df['close'].rolling(window=20).mean().bfill().tolist()
    ma60 = df['close'].rolling(window=60).mean().bfill().tolist()

    # ECharts K线数据: [open, close, low, high]
    kline_data = []
    for _, row in df.iterrows():
        try:
            kline_data.append([
                round(float(row['open']), 2),
                round(float(row['close']), 2),
                round(float(row['low']), 2),
                round(float(row['high']), 2)
            ])
        except (ValueError, TypeError):
            continue

    # 成交量数据
    volumes = []
    for i, row in df.iterrows():
        color = 1 if row['close'] >= row['open'] else -1
        try:
            vol = float(row.get('volume', 0))
        except (ValueError, TypeError):
            vol = 0.0
        volumes.append([i, round(vol, 2), color])

    return {
        'dates': df['date'].tolist(),
        'kline': kline_data,
        'ma5': ma5,
        'ma20': ma20,
        'ma60': ma60,
        'volumes': volumes,
    }


def generate_html_report(date_str: str) -> str:
    """生成完整的HTML报告（一屏统览风格）"""
    from database import load_scored_top
    top_df = load_scored_top(date_str, MOMENTUM_TOP_N)
    if top_df.empty:
        print(f"  [WARN] 数据库中无 {date_str} 的评分数据")
        return ""

    print(f"{'='*50}")
    print(f"  短线助手 — 可视化报告生成")
    print(f"  日期: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  K线回看天数: {KLINE_DAYS}日")
    print(f"{'='*50}")

    stocks_data = []
    summary_rows = []

    for i, row in top_df.iterrows():
        code = str(row['code']).zfill(6)
        name = row.get('name', '')
        rank = i + 1
        composite = row['composite_score']
        momentum = row['momentum_score']
        slope = row.get('trend_slope', 0)
        r2 = row.get('trend_r2', 0)

        print(f"  [{rank}/{len(top_df)}] {code} {name} — 生成图表中...")
        time.sleep(0.5)

        # 获取K线数据
        kline = fetch_kline_data(code, KLINE_DAYS, date_str)
        echarts_data = build_kline_echarts_data(kline) if not kline.empty else {}

        # 动量数据
        hist = fetch_kline_data(code, MOMENTUM_DAYS, date_str)
        momentum_data = None
        if not hist.empty and '收盘' in hist.columns:
            momentum_data = compute_momentum_for_display(hist['收盘'])
        else:
            momentum_data = {
                'days': [0], 'prices': [1], 'relative': [1],
                'trend_line': [1], 'momentum': 0, 'slope': 0, 'r2': 0
            }

        # 计算期间涨跌幅
        period_change = 0
        if not hist.empty and len(hist) >= 2:
            first_close = hist['收盘'].iloc[0]
            last_close = hist['收盘'].iloc[-1]
            period_change = (last_close / first_close - 1) * 100

        stocks_data.append({
            'rank': rank,
            'code': code,
            'name': name,
            'composite': composite,
            'momentum': momentum,
            'slope': slope,
            'r2': r2,
            'period_change': period_change,
            'echarts_data': echarts_data,
            'momentum_data': momentum_data,
        })

        summary_rows.append({
            'rank': rank,
            'name': name,
            'code': code,
            'composite': round(composite, 4),
            'momentum': round(momentum, 2),
            'period_change': f"{period_change:+.2f}%",
            'slope': round(slope, 6),
            'r2': round(r2, 4),
        })

    # 汇总统计
    total_stocks = len(stocks_data)
    avg_momentum = np.mean([s['momentum'] for s in stocks_data]) if stocks_data else 0
    best_stock = max(stocks_data, key=lambda x: x['momentum']) if stocks_data else None

    report_date = datetime.strptime(date_str, '%Y%m%d').strftime('%Y-%m-%d')

    # 构建股票卡片HTML
    stock_cards_html = []
    for s in stocks_data:
        echarts_json = json.dumps(s['echarts_data'], ensure_ascii=False)
        momentum_json = json.dumps({
            'days': s['momentum_data']['days'],
            'relative': s['momentum_data']['relative'],
            'trend_line': s['momentum_data']['trend_line'],
            'prices': s['momentum_data']['prices'],
        }, ensure_ascii=False)

        # 排名颜色
        rank_color = '#e74c3c' if s['rank'] == 1 else '#f39c12' if s['rank'] == 2 else '#27ae60' if s['rank'] == 3 else '#3498db'
        period_color = '#e74c3c' if s['period_change'] >= 0 else '#2ecc71'

        card_html = f'''
        <div class="stock-card" id="stock-{s['code']}">
            <div class="stock-header">
                <div class="rank-badge" style="background:{rank_color}">#{s['rank']}</div>
                <div class="stock-info">
                    <div class="stock-name">{s['name']}</div>
                    <div class="stock-code">{s['code']}</div>
                </div>
                <div class="stock-tags">
                    <span class="tag tag-composite">综合 {s['composite']:.4f}</span>
                    <span class="tag tag-momentum">动量 {s['momentum']:.2f}</span>
                    <span class="tag tag-change" style="background:{period_color}20;color:{period_color}">期间 {s['period_change']:+.2f}%</span>
                </div>
            </div>
            <div class="charts-row">
                <div class="chart-container">
                    <div class="chart-title">K线图（过去{KLINE_DAYS}日）</div>
                    <div class="kline-chart" id="kline-{s['code']}" style="width:100%;height:320px;"></div>
                </div>
                <div class="chart-container">
                    <div class="chart-title">动量分析（过去{MOMENTUM_DAYS}日）</div>
                    <div class="momentum-chart" id="momentum-{s['code']}" style="width:100%;height:320px;"></div>
                </div>
            </div>
        </div>
        <script>
        (function(){{
            var echartsData = {echarts_json};
            var momData = {momentum_json};
            var code = '{s['code']}';

            // K线图
            if(echartsData && echartsData.dates && echartsData.dates.length > 0){{
                var klineChart = echarts.init(document.getElementById('kline-' + code));
                var klineOption = {{
                    tooltip: {{
                        trigger: 'axis',
                        axisPointer: {{ type: 'cross' }},
                        formatter: function(params) {{
                            var d = params[0];
                            var idx = d.dataIndex;
                            var k = echartsData.kline[idx];
                            var date = echartsData.dates[idx];
                            return date + '<br/>开盘: ' + k[0] + '<br/>收盘: ' + k[1] + '<br/>最低: ' + k[2] + '<br/>最高: ' + k[3];
                        }}
                    }},
                    grid: [{{ left: '8%', right: '4%', top: '10%', height: '55%' }},
                           {{ left: '8%', right: '4%', top: '72%', height: '20%' }}],
                    xAxis: [
                        {{ type: 'category', data: echartsData.dates, scale: true, boundaryGap: false, axisLine: {{ onZero: false }}, splitLine: {{ show: false }}, min: 'dataMin', max: 'dataMax', axisLabel: {{ show: false }} }},
                        {{ type: 'category', gridIndex: 1, data: echartsData.dates, scale: true, boundaryGap: false, axisLine: {{ onZero: false }}, axisTick: {{ show: false }}, splitLine: {{ show: false }}, axisLabel: {{ show: true, rotate: 45, fontSize: 10 }}, min: 'dataMin', max: 'dataMax' }}
                    ],
                    yAxis: [
                        {{ scale: true, splitArea: {{ show: true }}, splitLine: {{ show: true, lineStyle: {{ color: '#eee' }} }} }},
                        {{ scale: true, gridIndex: 1, splitNumber: 2, axisLabel: {{ show: false }}, axisLine: {{ show: false }}, axisTick: {{ show: false }}, splitLine: {{ show: false }} }}
                    ],
                    dataZoom: [{{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }}],
                    series: [
                        {{
                            name: 'K线', type: 'candlestick',
                            data: echartsData.kline,
                            itemStyle: {{ color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' }}
                        }},
                        {{ name: 'MA5', type: 'line', data: echartsData.ma5, smooth: true, lineStyle: {{ color: '#ffb74d', width: 1.5 }}, showSymbol: false }},
                        {{ name: 'MA20', type: 'line', data: echartsData.ma20, smooth: true, lineStyle: {{ color: '#42a5f5', width: 1.5 }}, showSymbol: false }},
                        {{ name: 'MA60', type: 'line', data: echartsData.ma60, smooth: true, lineStyle: {{ color: '#ce93d8', width: 1.5 }}, showSymbol: false }},
                        {{
                            name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
                            data: echartsData.volumes.map(function(v) {{ return v[1]; }}),
                            itemStyle: {{
                                color: function(params) {{
                                    var c = echartsData.volumes[params.dataIndex][2];
                                    return c > 0 ? '#ef5350' : '#26a69a';
                                }}
                            }}
                        }}
                    ]
                }};
                klineChart.setOption(klineOption);
                window.addEventListener('resize', function() {{ klineChart.resize(); }});
            }} else {{
                document.getElementById('kline-' + code).innerHTML = '<div class="no-data">K线数据不足</div>';
            }}

            // 动量图
            if(momData && momData.days && momData.days.length > 0){{
                var momChart = echarts.init(document.getElementById('momentum-' + code));
                var colors = momData.prices.map(function(p, i) {{
                    return i === 0 ? '#91cc75' : (p >= momData.prices[i-1] ? '#ef5350' : '#26a69a');
                }});
                var momOption = {{
                    tooltip: {{ trigger: 'axis' }},
                    grid: [{{ left: '8%', right: '4%', top: '10%', height: '55%' }},
                           {{ left: '8%', right: '4%', top: '72%', height: '20%' }}],
                    xAxis: [
                        {{ type: 'category', data: momData.days, axisLabel: {{ show: false }} }},
                        {{ type: 'category', gridIndex: 1, data: momData.days, axisLabel: {{ show: true, fontSize: 10 }} }}
                    ],
                    yAxis: [
                        {{ type: 'value', name: '相对价格', splitLine: {{ show: true, lineStyle: {{ color: '#eee' }} }} }},
                        {{ type: 'value', gridIndex: 1, name: '收盘价', splitLine: {{ show: false }} }}
                    ],
                    series: [
                        {{
                            name: '相对价格', type: 'line', data: momData.relative,
                            lineStyle: {{ color: '#5470c6', width: 2 }}, symbol: 'circle', symbolSize: 4
                        }},
                        {{
                            name: '趋势线', type: 'line', data: momData.trend_line,
                            lineStyle: {{ color: '#ee6666', width: 2, type: 'dashed' }}, showSymbol: false
                        }},
                        {{
                            name: '收盘价', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
                            data: momData.prices, itemStyle: {{ color: function(p) {{ return colors[p.dataIndex]; }} }}
                        }}
                    ]
                }};
                momChart.setOption(momOption);
                window.addEventListener('resize', function() {{ momChart.resize(); }});
            }} else {{
                document.getElementById('momentum-' + code).innerHTML = '<div class="no-data">动量数据不足</div>';
            }}
        }})();
        </script>
        '''
        stock_cards_html.append(card_html)

    # 汇总表格行
    table_rows = []
    for r in summary_rows:
        change_color = 'color:#e74c3c;' if float(r['period_change'].replace('%', '')) >= 0 else 'color:#2ecc71;'
        table_rows.append(f'''
        <tr>
            <td><span class="table-rank rank-{r['rank']}">{r['rank']}</span></td>
            <td><strong>{r['name']}</strong></td>
            <td class="text-muted">{r['code']}</td>
            <td>{r['composite']}</td>
            <td><strong>{r['momentum']}</strong></td>
            <td style="{change_color}">{r['period_change']}</td>
            <td>{r['slope']}</td>
            <td>{r['r2']}</td>
        </tr>
        ''')

    # 组装完整HTML
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>短线助手 — 强势股筛选报告 {report_date}</title>
    <script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background: #f5f7fa;
            color: #333;
            line-height: 1.6;
        }}
        .container {{ max-width: 1480px; margin: 0 auto; padding: 20px; }}

        /* 页面头部 */
        .page-header {{
            background: linear-gradient(135deg, #2c3e50 0%, #34495e 50%, #4a6741 100%);
            color: white;
            padding: 32px 40px;
            border-radius: 16px;
            margin-bottom: 24px;
            text-align: center;
            box-shadow: 0 4px 20px rgba(0,0,0,0.15);
        }}
        .page-header h1 {{ font-size: 32px; font-weight: 700; margin-bottom: 8px; }}
        .page-header .subtitle {{ font-size: 14px; opacity: 0.85; }}

        /* 统计卡片 */
        .stats-bar {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 24px;
        }}
        .stat-card {{
            background: white;
            border-radius: 12px;
            padding: 24px 20px;
            text-align: center;
            box-shadow: 0 2px 12px rgba(0,0,0,0.06);
            transition: transform 0.2s;
        }}
        .stat-card:hover {{ transform: translateY(-2px); }}
        .stat-card .stat-value {{
            font-size: 36px;
            font-weight: 700;
            color: #2c3e50;
            margin-bottom: 4px;
        }}
        .stat-card .stat-label {{ font-size: 13px; color: #888; }}
        .stat-card.best .stat-value {{ color: #e74c3c; }}

        /* 最佳股票高亮 */
        .best-stock-banner {{
            background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
            color: white;
            border-radius: 16px;
            padding: 28px 40px;
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 4px 20px rgba(231, 76, 60, 0.3);
        }}
        .best-stock-banner .best-label {{ font-size: 14px; opacity: 0.9; margin-bottom: 4px; }}
        .best-stock-banner .best-name {{ font-size: 28px; font-weight: 700; }}
        .best-stock-banner .best-code {{ font-size: 14px; opacity: 0.8; }}
        .best-stock-banner .best-metrics {{
            display: flex;
            gap: 32px;
        }}
        .best-stock-banner .best-metric {{
            text-align: center;
        }}
        .best-stock-banner .best-metric-value {{
            font-size: 28px;
            font-weight: 700;
        }}
        .best-stock-banner .best-metric-label {{
            font-size: 12px;
            opacity: 0.8;
        }}

        /* 汇总表格 */
        .summary-section {{
            background: white;
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 24px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        }}
        .summary-section h2 {{
            font-size: 18px;
            margin-bottom: 16px;
            color: #2c3e50;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .summary-section h2::before {{
            content: '';
            width: 4px;
            height: 20px;
            background: #e74c3c;
            border-radius: 2px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }}
        th {{
            background: #f8f9fa;
            padding: 12px 14px;
            text-align: center;
            font-weight: 600;
            color: #555;
            border-bottom: 2px solid #e0e0e0;
            white-space: nowrap;
        }}
        td {{
            padding: 12px 14px;
            text-align: center;
            border-bottom: 1px solid #f0f0f0;
        }}
        tr:hover td {{ background: #fafbfc; }}
        .text-muted {{ color: #888; }}
        .table-rank {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px;
            height: 28px;
            border-radius: 50%;
            font-weight: 700;
            font-size: 13px;
            color: white;
        }}
        .table-rank.rank-1 {{ background: #e74c3c; }}
        .table-rank.rank-2 {{ background: #f39c12; }}
        .table-rank.rank-3 {{ background: #27ae60; }}
        .table-rank {{ background: #bdc3c7; }}

        /* 个股卡片 */
        .stock-card {{
            background: white;
            border-radius: 16px;
            padding: 20px 24px;
            margin-bottom: 20px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.06);
        }}
        .stock-header {{
            display: flex;
            align-items: center;
            gap: 14px;
            margin-bottom: 16px;
            padding-bottom: 14px;
            border-bottom: 1px solid #f0f0f0;
        }}
        .rank-badge {{
            color: white;
            font-weight: 700;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 16px;
            flex-shrink: 0;
        }}
        .stock-info {{ flex: 1; }}
        .stock-name {{ font-size: 20px; font-weight: 700; color: #2c3e50; }}
        .stock-code {{ color: #888; font-size: 13px; }}
        .stock-tags {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .tag {{
            padding: 5px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }}
        .tag-composite {{ background: #e3f2fd; color: #1565c0; }}
        .tag-momentum {{ background: #fce4ec; color: #c62828; }}
        .tag-change {{ font-weight: 700; }}

        .charts-row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}
        .chart-container {{
            background: #fafbfc;
            border-radius: 12px;
            padding: 12px;
        }}
        .chart-title {{
            font-size: 13px;
            font-weight: 600;
            color: #666;
            margin-bottom: 8px;
            padding-left: 4px;
        }}
        .no-data {{
            display: flex;
            align-items: center;
            justify-content: center;
            height: 100%;
            color: #999;
            font-size: 16px;
        }}

        @media (max-width: 1100px) {{
            .stats-bar {{ grid-template-columns: repeat(2, 1fr); }}
            .charts-row {{ grid-template-columns: 1fr; }}
            .best-stock-banner {{ flex-direction: column; gap: 16px; text-align: center; }}
        }}
        @media (max-width: 600px) {{
            .stats-bar {{ grid-template-columns: 1fr; }}
            .stock-header {{ flex-wrap: wrap; }}
        }}

        /* 页脚 */
        .page-footer {{
            text-align: center;
            padding: 24px;
            color: #999;
            font-size: 12px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- 页面头部 -->
        <div class="page-header">
            <h1>短线助手 — 强势股筛选报告</h1>
            <div class="subtitle">基于成交额 × 热度交集筛选 + {MOMENTUM_DAYS}日线性回归动量分析 | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>

        <!-- 统计卡片 -->
        <div class="stats-bar">
            <div class="stat-card">
                <div class="stat-value">{total_stocks}</div>
                <div class="stat-label">总股票数</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{total_stocks}</div>
                <div class="stat-label">成功分析</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{avg_momentum:.2f}</div>
                <div class="stat-label">平均动量评分</div>
            </div>
            <div class="stat-card best">
                <div class="stat-value">{best_stock['name'] if best_stock else '—'}</div>
                <div class="stat-label">最佳动量股</div>
            </div>
        </div>
'''

    # 最佳股票高亮横幅
    if best_stock:
        html += f'''
        <!-- 最佳股票高亮 -->
        <div class="best-stock-banner">
            <div>
                <div class="best-label">最佳动量股</div>
                <div class="best-name">{best_stock['name']}</div>
                <div class="best-code">{best_stock['code']}</div>
            </div>
            <div class="best-metrics">
                <div class="best-metric">
                    <div class="best-metric-value">#{best_stock['rank']}</div>
                    <div class="best-metric-label">动量排名</div>
                </div>
                <div class="best-metric">
                    <div class="best-metric-value">{best_stock['momentum']:.2f}</div>
                    <div class="best-metric-label">动量评分</div>
                </div>
                <div class="best-metric">
                    <div class="best-metric-value">{best_stock['period_change']:+.2f}%</div>
                    <div class="best-metric-label">期间涨跌</div>
                </div>
            </div>
        </div>
'''

    # 汇总表格 + 个股卡片
    html += f'''
        <!-- 汇总表格 -->
        <div class="summary-section">
            <h2>TOP{MOMENTUM_TOP_N} 强势股汇总</h2>
            <div style="overflow-x:auto;">
                <table>
                    <thead>
                        <tr>
                            <th>排名</th>
                            <th>名称</th>
                            <th>代码</th>
                            <th>综合评分</th>
                            <th>动量评分</th>
                            <th>期间涨跌</th>
                            <th>趋势斜率</th>
                            <th>趋势R²</th>
                        </tr>
                    </thead>
                    <tbody>
                        {''.join(table_rows)}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 个股详情 -->
        <div class="summary-section">
            <h2>个股K线 & 动量分析</h2>
        </div>
        {''.join(stock_cards_html)}

        <div class="page-footer">
            <p>短线助手 shortais | 存储: SQLite | 数据来源: dcsdk / pywencai / akshare | 仅供参考，不构成投资建议</p>
        </div>
    </div>
</body>
</html>'''

    output_path = os.path.join(OUTPUT_DIR, date_str, 'report.html')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n[OK] 报告已生成: {output_path}")
    return output_path


def main():
    today = datetime.now().strftime('%Y%m%d')
    path = generate_html_report(today)
    print(f"\n>> 用浏览器打开: {path}")


if __name__ == '__main__':
    main()

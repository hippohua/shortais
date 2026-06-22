# 短线助手 (shortais) — A股强势股筛选系统

> 基于 "资金 × 人气 × 动量" 三维选股模型的自动化短线强势股筛选工具
>
> 3分钟完成从数据获取到投资决策参考的全流程

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 需求背景](#2-需求背景)
- [3. 核心算法与计算逻辑](#3-核心算法与计算逻辑)
- [4. 系统架构](#4-系统架构)
- [5. 项目结构](#5-项目结构)
- [6. 模块详解](#6-模块详解)
- [7. 数据流](#7-数据流)
- [8. API 接口](#8-api-接口)
- [9. 配置参数](#9-配置参数)
- [10. 快速开始](#10-快速开始)
- [11. 使用说明](#11-使用说明)
- [12. 技术栈](#12-技术栈)
- [13. 数据库设计](#13-数据库设计)
- [14. 性能指标](#14-性能指标)
- [15. 风险与局限](#15-风险与局限)
- [16. 未来扩展](#16-未来扩展)

---

## 1. 项目概述

### 1.1 一句话描述

短线助手是一套 **Web 应用 + CLI 工具**，每日自动从 A 股全市场 5000+ 股票中，通过成交额 × 热度交集筛选 + 线性回归动量评分，输出 **最具短线爆发力的 TOP 10 强势股**，并生成交互式可视化 Web 报告。

### 1.2 核心设计理念

| 维度 | 含义 | 衡量指标 | 为什么重要 |
|------|------|----------|------------|
| **资金** | 大资金介入是短线拉升的前提 | 当日成交额排名 TOP100 | 有资金没人气 → 庄股风险 |
| **人气** | 市场关注度决定跟风意愿 | 当日个股热度排名 TOP100 | 有人气没资金 → 散户自嗨 |
| **强势** | 趋势已确立且持续性强的标的 | 25日线性回归动量评分 | 有趋势没资金人气 → 阴涨不爆 |

**三者缺一不可**：只有同时满足三个维度的股票，才具备短线爆发的必要条件。

---

## 2. 需求背景

A 股市场超过 5000 只股票，短线交易者需要在海量标的中快速定位兼具资金活跃度、市场人气和技术面强势的个股。人工逐一筛选效率极低且容易遗漏关键标的。

本项目解决的问题：
- ✅ **自动获取**：无需手动浏览行情软件，程序自动拉取成交额和热度数据
- ✅ **智能筛选**：多级漏斗式筛选，从 5000+ → 100 → ~50 → 30 → 10
- ✅ **量化评分**：线性回归动量评分，兼顾趋势强度与可靠性
- ✅ **可视化呈现**：交互式 K 线图 + 动量分析图，一目了然
- ✅ **数据持久化**：SQLite 存储历史数据，支持缓存跳过重复计算
- ✅ **Web 界面**：一键运行，实时进度反馈

---

## 3. 核心算法与计算逻辑

### 3.1 整体流程

```
全市场 5000+ 股票
      │
      ├──→ pywencai "成交额前100" ──→ volume_df (100只)
      │                                      │
      ├──→ pywencai "个股热度前100" ──→ hot_df (100只)
      │                                      │
      │                        ┌─ 取交集 ────┘
      │                        │  (~50只)
      │                        ▼
      │              Min-Max 标准化 + 综合评分
      │                        │
      │                  取前30 (FINAL_TOP_N)
      │                        │
      │           mootdx/akshare 获取25日K线
      │                        │
      │              线性回归动量评分 (slope × R² × 10000)
      │                        │
      │                  取前10 (MOMENTUM_TOP_N)
      │                        │
      │          ┌─────────────┴─────────────┐
      │          ▼                           ▼
      │    数据库持久化                  Web 可视化报告
```

### 3.2 Phase A：资金 × 人气 交集筛选

**步骤 1：取交集**

取成交额 TOP100 和热度 TOP100 的股票代码交集，确保选出的股票既有资金又有热度。

**步骤 2：Min-Max 标准化**

成交额单位为元（十亿级别），热度值为无量纲指数（百万级别），数值范围差异巨大。直接相加会导致成交额完全压倒热度。

$$vol_{norm} = \frac{vol - vol_{min}}{vol_{max} - vol_{min}}$$

$$hot_{norm} = \frac{hot - hot_{min}}{hot_{max} - hot_{min}}$$

**步骤 3：综合评分**

$$score_{composite} = \frac{vol_{norm} + hot_{norm}}{2}$$

按综合评分降序取前 30 只进入动量分析。

### 3.3 Phase B：线性回归动量评分

**为什么用线性回归？**

- 纯价格涨幅会被单日暴涨误导
- 回归线综合考虑了一段时间的整体走势质量
- slope × R² 确保只有 **既强劲又稳定** 的上涨趋势才能得高分

**计算过程：**

1. 获取最近 25 个交易日的收盘价序列
2. 计算相对价格（以第一天为基准）：

   $$p_{rel}[i] = \frac{close[i]}{close[0]}$$

3. 线性回归拟合：

   $$y = slope \times x + intercept$$

   其中 $x = [0, 1, 2, ..., 24]$，$y =$ 相对价格

4. **动量评分**：

   $$momentum = 10000 \times slope \times R^2$$

| 因子 | 含义 | 说明 |
|------|------|------|
| slope（斜率） | 趋势强度 | 斜率越大，价格上涨越快 |
| R²（决定系数） | 趋势可靠性 | R² 越接近 1，走势越"规整" |
| × 10000 | 缩放系数 | 将结果放大到可读范围 |

---

## 4. 系统架构

### 4.1 架构图

```
┌─────────────────────────────────────────────────────────┐
│                     Web 前端 (SPA)                        │
│  ECharts 图表 | SSE 实时进度 | 一键运行 | 历史查询        │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP + SSE
┌─────────────────────▼───────────────────────────────────┐
│                    Flask Web 应用                         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌─────────┐ │
│  │ /api/run │  │/api/status│ │/api/results│ │/api/rank1│ │
│  │ 触发分析  │  │ 运行状态  │  │ 结果查询  │  │ 排名历史 │ │
│  └──────────┘  └──────────┘  └──────────┘  └─────────┘ │
│                                                          │
│  ┌───────────────────────────────────────────────────┐  │
│  │              4 步流水线 (后台线程)                   │  │
│  │  Step1: 数据获取 → Step2: 筛选评分 → Step3: 图表生成 │  │
│  │  → Step4: 完成存储                                  │  │
│  └───────────────────────────────────────────────────┘  │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                   业务逻辑层                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  get_data.py  │  │ screener.py  │  │visualizer.py │  │
│  │  数据获取      │  │  筛选评分     │  │  图表生成     │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  │
│         │                 │                 │           │
│  ┌──────▼─────────────────▼─────────────────▼───────┐  │
│  │                  database.py                      │  │
│  │        SQLite 持久化 + 缓存管理                    │  │
│  └──────────────────────────────────────────────────┘  │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                    外部数据源                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │ pywencai  │  │  mootdx   │  │ akshare  │              │
│  │ 选股/排名  │  │ K线(TCP)  │  │ 兜底数据  │              │
│  └──────────┘  └──────────┘  └──────────┘              │
└─────────────────────────────────────────────────────────┘
```

### 4.2 设计原则

| 原则 | 实现方式 |
|------|----------|
| **模块解耦** | 各模块独立可运行，通过数据库交换数据 |
| **数据优先缓存** | 当天数据已入库则跳过 API 调用，大幅减少请求频率 |
| **多级回退** | 数据源按 pywencai → akshare 回退；K 线按 mootdx → akshare 回退 |
| **容错设计** | 单只股票失败不阻塞整体流程，API 请求含重试机制 |
| **配置集中** | 所有可调参数集中在 `config.py`，修改后无需改动业务代码 |

---

## 5. 项目结构

```
shortais/
├── app.py                # Flask Web 应用主入口（含前端 SPA）
├── config.py             # 全局配置参数
├── database.py           # SQLite 数据库模块（CRUD + 缓存）
├── get_data.py           # 数据获取模块（pywencai + akshare 兜底）
├── screener.py           # 筛选与动量评分模块
├── visualizer.py         # 可视化图表生成模块
├── requirements.txt      # Python 依赖清单
├── README.md             # 项目文档
├── 需求文档.md            # 原始需求文档
├── 网上关于 A 股数据源的文章.doc  # 数据源调研
│
└── data/                 # 数据目录（运行时生成）
    ├── shortais.db       # SQLite 数据库
    └── YYYYMMDD/         # 按日期归档的输出文件
        ├── raw_data.xlsx # 原始数据（可选）
        └── report.html   # HTML 可视化报告（可选）
```

### 模块依赖关系

```
config.py ───── 被所有模块引用
database.py ─── 被 app.py, screener.py, visualizer.py 引用
get_data.py ─── 被 app.py 引用
screener.py ─── 被 app.py 引用，依赖 database.py
visualizer.py ─ 被 app.py 引用，依赖 database.py
app.py ──────── 主入口，调度所有模块
```

---

## 6. 模块详解

### 6.1 `config.py` — 全局配置

```python
VOLUME_TOP_N = 100       # 成交额排名取前 N 只
HOT_TOP_N = 100          # 热度排名取前 N 只
FINAL_TOP_N = 30         # 综合评分取前 N 名进入动量分析
MOMENTUM_DAYS = 25       # 动量计算回看天数（约 5 周）
MOMENTUM_TOP_N = 10      # 最终输出股票数量
KLINE_DAYS = 60          # K 线图回看天数（约 3 个月）
OUTPUT_DIR = "data"      # 数据输出根目录
DB_PATH = "data/shortais.db"  # SQLite 数据库路径
```

### 6.2 `get_data.py` — 数据获取模块

**功能**：获取全市场成交额排名和热度排名数据。

**数据源优先级**：

| 优先级 | 数据源 | 用途 | 备注 |
|--------|--------|------|------|
| 1 | pywencai | 自然语言查询成交额/热度排名 | 主数据源 |
| 2 | 同花顺热点接口 | 补充题材标签 | 非阻塞 |
| 3 | akshare | 兜底数据源 | 底层打东方财富，有反爬风险 |

**核心函数**：

| 函数 | 功能 |
|------|------|
| `fetch_data(date_str)` | 主入口，多级回退获取数据 |
| `fetch_volume_top_wencai(date_str)` | pywencai 获取成交额排名 |
| `fetch_hot_top_wencai(date_str)` | pywencai 获取热度排名 |
| `fetch_hotspot_10jqka()` | 同花顺热点标签补充 |
| `fetch_volume_top_akshare(date_str)` | akshare 兜底获取成交额 |
| `fetch_hot_top_akshare(date_str)` | akshare 兜底获取热度 |
| `standardize_columns(df, data_type)` | 列名标准化 |
| `_retry_call(fn)` | 带重试的函数调用（3次） |

**列名标准化映射**：

```
pywencai 返回列名              → 标准化列名
股票简称                       → name
成交额[YYYYMMDD]               → volume_amount
成交额排名[YYYYMMDD]           → rank
个股热度[YYYYMMDD]             → hot_value
个股热度排名[YYYYMMDD]         → hot_rank
涨跌幅[:YYYYMMDD]              → pct_change
最新价[YYYYMMDD]               → close
```

### 6.3 `screener.py` — 筛选与动量评分模块

**功能**：执行交集筛选 + 综合评分 + 动量评分，输出最终强势股名单。

**K 线数据源优先级**：

| 优先级 | 数据源 | 协议 | 特点 |
|--------|--------|------|------|
| 1 | mootdx | 通达信 TCP | 零鉴权、不封 IP、速度快 |
| 2 | akshare | HTTP | 兜底、有反爬风险 |

**核心函数**：

| 函数 | 功能 |
|------|------|
| `load_raw_data(date_str)` | 从数据库加载原始数据 |
| `min_max_normalize(series)` | Min-Max 标准化到 [0, 1] |
| `compute_composite_score(vol_df, hot_df)` | 交集 + 标准化 + 综合评分 |
| `fetch_stock_history(code, days, end_date)` | 获取个股历史日线（mootdx → akshare 回退） |
| `compute_momentum_score(prices)` | 线性回归动量评分计算 |
| `score_momentum_batch(candidates, date_str)` | 批量动量评分 |

**mootdx 连接管理**：
- 单例连接池，自动重连
- 连接验证带 12 秒超时保护
- 3 次重试，指数退避
- 双 frequency 尝试（先 9 后 4）

### 6.4 `visualizer.py` — 可视化模块

**功能**：生成 K 线数据和动量分析数据，支持 ECharts 渲染。

**核心函数**：

| 函数 | 功能 |
|------|------|
| `fetch_kline_data(code, days, end_date)` | 获取 K 线数据（mootdx → akshare 回退） |
| `compute_momentum_for_display(prices)` | 计算动量指标并返回画图数据 |
| `build_kline_echarts_data(kline_df)` | 将 K 线数据转换为 ECharts 格式 |
| `generate_html_report(date_str)` | 生成完整 HTML 报告 |

**图表内容**：
- **K 线图**：60 日 OHLC 蜡烛图 + MA5/MA20/MA60 均线 + 成交量柱状图
- **动量分析图**：相对价格走势 + 线性回归趋势线（虚线） + 收盘价柱状图

### 6.5 `database.py` — 数据库模块

**功能**：SQLite 数据持久化，支持缓存命中跳过重复计算。

**数据表设计**：

| 表名 | 用途 | 主键 | 自动建表 |
|------|------|------|----------|
| `raw_volume` | 成交额排名原始数据 | — | pandas to_sql() |
| `raw_hot` | 热度排名原始数据 | — | pandas to_sql() |
| `scored_stocks` | 评分结果（全部 + TOP） | — | pandas to_sql() |
| `chart_cache` | 图表 JSON 缓存 | date | 手动建表 |
| `rank1_history` | 每日排名第一历史 | date | 手动建表 |

**核心函数**：

| 函数 | 功能 |
|------|------|
| `init_db()` | 建表 + 清理残留空表（幂等） |
| `has_raw_data(date)` | 检查当天原始数据是否已入库 |
| `save_raw_volume(date, df)` | 保存成交额排名（先删后插） |
| `save_raw_hot(date, df)` | 保存热度排名（先删后插） |
| `has_scored_data(date)` | 检查当天评分是否已入库 |
| `save_scored_stocks(date, all_df, top_df)` | 保存评分结果（自动补列） |
| `has_chart_cache(date)` | 检查图表缓存是否存在 |
| `save_chart_cache(date, data)` | 保存图表 JSON blob |
| `load_chart_cache(date)` | 读取图表缓存 |
| `save_rank1(date, row)` | 保存当日排名第一 |
| `load_rank1_history(days)` | 读取近 N 天排名第一历史 |

### 6.6 `app.py` — Web 应用主入口

**功能**：Flask Web 服务，内嵌 SPA 前端，调度 4 步流水线。

**4 步流水线**：

```
Step 1: 数据获取 ─── _step1_fetch_data()
  ├── 检查缓存 → 命中则从数据库加载
  └── 未命中 → 调用 get_data.fetch_data() → 存入数据库

Step 2: 筛选评分 ─── _step2_screen()
  ├── 检查缓存 → 命中则从数据库加载
  └── 未命中 → load_raw_data() → compute_composite_score() → score_momentum_batch()

Step 3: 图表生成 ─── _step3_generate_charts()
  ├── 检查缓存 → 命中则从数据库加载
  └── 未命中 → generate_chart_data() → 获取 K 线 → 计算动量 → 存入缓存

Step 4: 完成存储 ─── _step4_finalize()
  └── 保存 rank1 历史 + 更新状态
```

**缓存策略**：每次运行时检查 3 个阶段的缓存状态，命中则跳过。如果缓存不完整（如 TOP 为空），自动回退到重新计算。

---

## 7. 数据流

### 7.1 完整数据流图

```
┌─────────────────────────────────────────────────────────────┐
│  数据获取 (Step 1)                                           │
│                                                              │
│  pywencai ──→ 成交额TOP100 ──→ volume_df                     │
│  pywencai ──→ 热度TOP100   ──→ hot_df                        │
│                          │                                   │
│                    ┌─────┴─────┐                             │
│                    │  SQLite   │ ← raw_volume / raw_hot      │
│                    └─────┬─────┘                             │
└──────────────────────────┼──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  筛选评分 (Step 2)                                           │
│                                                              │
│  volume_df + hot_df                                          │
│       │                                                      │
│       ├── 取交集 (~50只)                                     │
│       ├── Min-Max 标准化                                      │
│       ├── 综合评分 → 取前30                                   │
│       │                                                      │
│       ├── mootdx TCP ──→ 25日K线 ──→ 线性回归 ──→ 动量评分   │
│       │   (失败时自动回退到 akshare)                          │
│       │                                                      │
│       └── 按动量排序 → 取前10                                 │
│                          │                                   │
│                    ┌─────┴─────┐                             │
│                    │  SQLite   │ ← scored_stocks             │
│                    └─────┬─────┘                             │
└──────────────────────────┼──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  图表生成 (Step 3)                                           │
│                                                              │
│  TOP10 stocks                                                │
│       │                                                      │
│       ├── mootdx ──→ 60日K线 → ECharts candlestick           │
│       ├── mootdx ──→ 25日K线 → 动量分析图                    │
│       │                                                      │
│       └── 组装 JSON → 存入 chart_cache                       │
│                          │                                   │
│                    ┌─────┴─────┐                             │
│                    │  SQLite   │ ← chart_cache (JSON blob)   │
│                    └─────┬─────┘                             │
└──────────────────────────┼──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│  Web 前端渲染                                                │
│                                                              │
│  /api/results/{date} ──→ JSON ──→ ECharts 渲染              │
│  /api/rank1-history  ──→ JSON ──→ 排名历史面板               │
│  /api/status/stream  ──→ SSE  ──→ 实时进度更新              │
└─────────────────────────────────────────────────────────────┘
```

### 7.2 多级回退机制

```
数据获取回退链:
  pywencai ──(失败)──→ akshare ──(失败)──→ 抛出异常

K线获取回退链:
  mootdx(freq=9) ──→ mootdx(freq=4) ──→ akshare ──→ 空数据

重试机制:
  每次失败自动重试（mootdx 3次，akshare 2次），指数退避等待
```

---

## 8. API 接口

| 路由 | 方法 | 功能 | 响应 |
|------|------|------|------|
| `/` | GET | 主页面（SPA） | HTML |
| `/api/last-run` | GET | 最近一次运行日期 | `{has_data, date}` |
| `/api/rank1-history` | GET | 近7天排名第一历史 | `{records: [...]}` |
| `/api/results/<date>` | GET | 指定日期分析结果 | `{date, stocks, stats}` |
| `/api/run` | POST | 触发重新运行 | `{success, message}` |
| `/api/status` | GET | 当前运行状态（轮询） | `{running, stage, progress, log}` |
| `/api/status/stream` | GET | SSE 实时状态推送 | `text/event-stream` |

### 8.1 SSE 事件类型

| type | 说明 |
|------|------|
| `log` | 日志行推送 |
| `progress` | 进度更新 |
| `step_data` | 步骤中间结果（step1/step2） |
| `error` | 错误信息 |
| `complete` | 分析完成 |
| `done` | 连接关闭 |

---

## 9. 配置参数

所有参数集中在 `config.py`，修改后无需改动业务代码：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `VOLUME_TOP_N` | 100 | 成交额排名取前 N 只 |
| `HOT_TOP_N` | 100 | 热度排名取前 N 只 |
| `FINAL_TOP_N` | 30 | 综合评分取前 N 名进入动量分析 |
| `MOMENTUM_DAYS` | 25 | 动量计算回看天数（约 5 周） |
| `MOMENTUM_TOP_N` | 10 | 最终输出股票数量 |
| `KLINE_DAYS` | 60 | K 线图回看天数（约 3 个月） |
| `OUTPUT_DIR` | "data" | 数据输出根目录 |
| `DB_PATH` | "data/shortais.db" | SQLite 数据库路径 |

---

## 10. 快速开始

### 10.1 环境要求

- Python 3.10+
- Windows / macOS / Linux

### 10.2 安装与运行

```bash
# 1. 克隆或进入项目目录
cd "d:/py project/shortais"

# 2. 安装依赖（仅首次）
pip install -r requirements.txt

# 3. 启动 Web 服务（自动打开浏览器）
python app.py

# 浏览器自动打开 http://127.0.0.1:5678
# 点击"重新运行"按钮即可开始分析
```

### 10.3 命令行独立运行

每个模块也可以独立运行：

```bash
# 仅获取数据
python get_data.py

# 仅筛选评分
python screener.py

# 仅生成 HTML 报告
python visualizer.py
```

---

## 11. 使用说明

### 11.1 Web 界面操作

1. **启动服务**：`python app.py`，浏览器自动打开
2. **查看历史结果**：页面自动加载最近一次分析结果
3. **重新运行**：点击"重新运行"按钮，实时查看 4 步进度
4. **查看报告**：包含汇总表格、K 线图、动量分析图、最佳股票高亮
5. **排名历史**：左侧面板显示近 7 天每日最佳动量股

### 11.2 运行时机

- **最佳时间**：每个交易日 **15:00 收盘后**
- **数据完整性**：pywencai 和同花顺热点接口收盘后数据最完整
- **缓存策略**：同一天多次运行不会重复调用 API，直接从数据库加载

### 11.3 数据目录

```
data/
├── shortais.db           # 主数据库（SQLite）
├── 20260610/             # 按日期归档
│   ├── raw_data.xlsx     # 原始数据
│   └── report.html       # HTML 报告
└── 20260611/
    └── ...
```

---

## 12. 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| Web 框架 | Flask | HTTP 服务 + API 路由 |
| 前端 | 原生 HTML/CSS/JS + ECharts 5 | SPA 单页应用 + 交互式图表 |
| 实时通信 | Server-Sent Events (SSE) | 运行进度实时推送 |
| 数据库 | SQLite (sqlite3) | 数据持久化 + 缓存 |
| 数据获取 | pywencai | 自然语言选股查询 |
| K 线数据 | mootdx (主) | 通达信 TCP 协议，零鉴权 |
| 兜底数据 | akshare | 东方财富接口兜底 |
| 数据处理 | pandas + numpy | DataFrame 操作、数值计算 |
| 机器学习 | scikit-learn (LinearRegression) | 线性回归动量评分 |
| 可视化 | ECharts 5 (CDN) | K 线图 + 动量分析图 |
| 文件格式 | openpyxl | Excel 读写（可选输出） |

---

## 13. 数据库设计

### 13.1 表结构

**raw_volume / raw_hot**（由 pandas to_sql 自动建表）：

| 列名 | 类型 | 说明 |
|------|------|------|
| date | TEXT | 日期 YYYYMMDD |
| code | TEXT | 6 位股票代码 |
| name | TEXT | 股票名称 |
| volume_amount / hot_value | REAL | 成交额 / 热度值 |
| rank / hot_rank | INTEGER | 排名 |
| pct_change | REAL | 涨跌幅 |
| ... | | 其他动态列 |

**scored_stocks**（由 pandas to_sql 自动建表）：

| 列名 | 类型 | 说明 |
|------|------|------|
| date | TEXT | 日期 |
| sheet | TEXT | 'top' 或 'all' |
| code | TEXT | 股票代码 |
| name | TEXT | 股票名称 |
| composite_score | REAL | 综合评分 |
| momentum_score | REAL | 动量评分 |
| trend_slope | REAL | 趋势斜率 |
| trend_r2 | REAL | 趋势 R² |
| momentum_rank | INTEGER | 动量排名 |
| final_rank | INTEGER | 最终排名（仅 TOP） |

**chart_cache**（手动建表）：

| 列名 | 类型 | 说明 |
|------|------|------|
| date | TEXT PRIMARY KEY | 日期 |
| chart_data | TEXT | JSON blob（K 线 + 动量数据） |

**rank1_history**（手动建表）：

| 列名 | 类型 | 说明 |
|------|------|------|
| date | TEXT PRIMARY KEY | 日期 |
| code | TEXT | 股票代码 |
| name | TEXT | 股票名称 |
| momentum_score | REAL | 动量评分 |
| period_change | REAL | 期间涨跌幅 |
| date_display | TEXT | 日期显示格式 MM-DD |

### 13.2 缓存策略

- **写入**：每次运行完成后写入当天数据，使用 INSERT OR REPLACE 确保幂等
- **读取**：每次运行前检查 3 个阶段缓存，命中则跳过 API 调用
- **清理**：`init_db()` 自动检测并删除残留空表

---

## 14. 性能指标

| 阶段 | 耗时（首次） | 耗时（缓存命中） |
|------|-------------|-----------------|
| Step 1: 数据获取 | 10-20 秒 | < 1 秒 |
| Step 2: 筛选评分 | 30-60 秒 | < 1 秒 |
| Step 3: 图表生成 | 30-90 秒 | < 1 秒 |
| Step 4: 完成 | < 1 秒 | < 1 秒 |
| **总计** | **1-3 分钟** | **< 3 秒** |

**耗时说明**：
- Step 1 耗时主要来自 2 次 pywencai API 调用
- Step 2 耗时主要来自 30 次 K 线数据 API 调用（每只候选股 0.15s 间隔）
- Step 3 耗时主要来自 10 次 K 线数据 API 调用（每只股票 0.5s 间隔）
- 缓存命中后几乎瞬间完成

---

## 15. 风险与局限

| 风险 | 影响 | 应对措施 |
|------|------|----------|
| pywencai API 失效 | 数据获取失败 | akshare 兜底；可扩展 baostock 等替代源 |
| mootdx 连接超时 | K 线数据缺失 | 3 次重试 + akshare 兜底 + 超时保护 |
| akshare 反爬 | K 线数据部分缺失 | 增加请求间隔 + 重试机制 |
| 非交易日运行 | 无当日数据 | 程序会尝试获取最新交易日数据 |
| 交集过小 | 筛选池不足 | 可调大 `VOLUME_TOP_N` 和 `HOT_TOP_N` |
| 数据列名变更 | 标准化失败 | `standardize_columns` 兼容多种列名格式 |
| 单只股票无数据 | 动量计算跳过 | 不影响其他股票，标注"无数据" |

---

## 16. 未来扩展

1. **定时任务**：集成 Windows 任务计划 / cron，每日 15:00 自动运行
2. **历史回测**：对历史每天的输出进行模拟交易回测，验证选股胜率
3. **更多因子**：换手率、量比、MACD 金叉等纳入综合评分
4. **通知推送**：飞书 / 钉钉 / Webhook 推送当日 TOP10 清单
5. **多市场支持**：港股、美股扩展
6. **策略对比**：不同参数组合的选股效果对比

---

## 附录

### A. 依赖清单

```
pywencai          # 问财数据 API
mootdx            # 通达信 TCP 行情（K 线主力）
akshare           # A 股历史行情（兜底）
pandas            # 数据处理
numpy             # 数值计算
openpyxl          # Excel 读写
scikit-learn      # 线性回归
flask             # Web 框架
requests          # HTTP 请求
```

### B. 常见问题

**Q: 运行报错 "交集为空"？**
A: 可能是当日数据源返回异常，删除数据库中当天数据后重新运行。

**Q: mootdx 连接失败？**
A: 程序会自动回退到 akshare，不影响最终结果。检查网络连接和防火墙设置。

**Q: 如何修改输出股票数量？**
A: 修改 `config.py` 中的 `MOMENTUM_TOP_N` 参数，默认 10。

**Q: 如何查看历史某天的结果？**
A: 在 Web 界面选择日期，或直接调用 `/api/results/20260610`。

---

<div align="center">

**短线助手 shortais** | 数据来源: pywencai / mootdx / akshare

⚠️ 仅供参考，不构成投资建议。投资有风险，入市需谨慎。

</div>

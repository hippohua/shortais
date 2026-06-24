# dcSdk — A股行情数据 SDK

> 多数据源自适应：腾讯行情(主力) + 新浪行情(备用) + 东方财富(基本面)
> 纯 Python 实现，仅依赖 `requests`

## 安装

### 方式一：拷贝目录
直接将 `dcsdk/` 目录拷贝到你的项目中。

### 方式二：pip 安装
```bash
pip install -e /path/to/dcsdk
# 或
pip install /path/to/dcsdk
```

## 快速开始

```python
from dcsdk import quote, kline, market_summary

# 个股实时行情
data = quote("600519")
print(data["name"], data["price"], data["change_pct"])

# K线数据（日线，最近100条）
klines = kline("600519", period="day", count=100)
for k in klines[-5:]:
    print(k["date"], k["close"])

# 大盘概况
market = market_summary()
for name, info in market.items():
    if isinstance(info, dict) and "change_pct" in info:
        print(f"{name}: {info['price']} ({info['change_pct']}%)")
```

## API 概览

### 函数式 API（一行调用）

| 函数 | 说明 |
|------|------|
| `quote(code)` | 个股实时行情 |
| `quote_batch(codes)` | 批量行情 |
| `kline(code, period, count)` | K线数据 |
| `market_indexes(names)` | 指定指数行情 |
| `market_summary()` | 大盘概况 |
| `board_industry(top_n)` | 行业板块排行 |
| `board_concept(top_n)` | 概念板块排行 |
| `limit_scan(codes)` | 涨跌停扫描 |

### 类式 API（高级用法）

```python
from dcsdk import StockQuote, KLine, Market, Board, MoneyFlow, LimitMonitor

# 实时行情
sq = StockQuote()
data = sq.get("600519")
batch = sq.get_batch(["600519", "000858"])

# K线
kl = KLine()
klines = kl.get("600519", "day", 200)

# 大盘
m = Market()
print(m.indexes())       # 全部指数
print(m.indexes(["上证指数", "创业板指"]))

# 板块排行
b = Board()
print(b.industry_ranking(10))
print(b.concept_ranking(10))

# 资金流向
mf = MoneyFlow()
print(mf.stock_detail("600519", market=1))
print(mf.north_bound())  # 北向资金

# 涨跌停监控
lm = LimitMonitor()
print(lm.scan())
print(lm.scan(["600519", "300750", "002594"]))
```

## 数据源

| 数据源 | 用途 | 可靠性 |
|--------|------|--------|
| 腾讯行情 (qt.gtimg.cn) | 实时行情、K线、指数 | ⭐⭐⭐ 主力 |
| 新浪行情 (hq.sinajs.cn) | 实时行情备用 | ⭐⭐ 备用 |
| 东方财富 (eastmoney.com) | 基本面、资金流向 | ⭐⭐ |

## 依赖

- Python >= 3.8
- requests >= 2.25
- urllib3 >= 1.26

## License

MIT

"""
短线助手 (shortais) — 配置参数
所有可调参数集中管理

数据源架构：
  首选: 同花顺扶摇金融数据API（fuyao.aicubes.cn）
  备选: 腾讯 qt.gtimg.cn（批量行情） + dcsdk（K线/板块）
  兜底: mootdx + akshare
"""

# ==================== 同花顺扶摇API ====================
FUYAO_API_KEY = "sk-fuyao-w0CCV8H2Nt4O2a7AAzoYWb8ULDxdh3Nc"  # 请填入你在 https://fuyao.aicubes.cn/admin/ 创建的 API Key
FUYAO_BASE_URL = "https://fuyao.aicubes.cn"

# ==================== 筛选参数 ====================
VOLUME_TOP_N = 100       # 成交额排名取前N只
HOT_TOP_N = 100          # 热度排名取前N只
FINAL_TOP_N = 30         # 综合评分取前N名
MOMENTUM_DAYS = 25       # 动量计算回看天数
MOMENTUM_TOP_N = 10      # 最终动量筛选输出多少只

# ==================== 输出配置 ====================
OUTPUT_DIR = "data"      # 数据输出目录
DB_PATH = "data/shortais.db"  # SQLite 数据库路径

# ==================== K线图配置 ====================
KLINE_DAYS = 60          # 可视化K线图回看天数

# ==================== 评分模型配置 ====================
# 修改该版本号会让当天旧评分缓存失效，避免公式升级后仍复用旧结果。
SCORING_VERSION = "cls_sector_v1"

# 最终评分 = 多因子加权 - 风险惩罚。各子分数均归一化到 0~1 后再乘以 100。
WEIGHT_VOLUME = 0.20         # 成交额排名分位
WEIGHT_HOT = 0.18            # 热度排名分位
WEIGHT_MOMENTUM = 0.34       # 风险调整动量排名分位
WEIGHT_PERIOD_STRENGTH = 0.13  # 区间涨幅排名分位
WEIGHT_TREND_QUALITY = 0.10  # 趋势R²质量
WEIGHT_LIQUIDITY = 0.05      # 流动性质量
WEIGHT_SECTOR = 0.12         # 行业板块强度

PENALTY_DRAWDOWN = 0.12      # 最大回撤惩罚权重
PENALTY_OVERHEAT = 0.16      # 过热惩罚权重
PENALTY_VOLATILITY = 0.08    # 波动惩罚权重

# 过热/风险阈值（小数形式，0.18 = 18%）
RECENT_SPIKE_3D_LIMIT = 0.18
RECENT_SPIKE_5D_LIMIT = 0.30
DAILY_SPIKE_LIMIT = 0.095
MAX_DRAWDOWN_LIMIT = 0.22
VOLATILITY_LIMIT = 0.045

# ==================== 板块强度配置 ====================
SECTOR_CACHE_TTL_HOURS = 4    # 板块数据缓存有效期，盘中可用“刷新数据并运行”更新
LIMIT_UP_THRESHOLD = 9.8      # 简化涨停阈值；创业板/科创板会在后续版本细分

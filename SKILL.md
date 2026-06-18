# SKILL.md — 股票量化推荐系统能力描述

## 系统定位

面向 A股+港股 的主动投资决策支撑系统。**不做自动交易**，只做分析、审查和候选推荐。用户是最终决策者，系统是共同思考者。

---

## 能力清单

### 1. 组合审查 (Portfolio Review)
实时审查持仓组合，输出每个持仓的盈亏、风险信号、买入逻辑有效性评估。

**入口：**
```bash
python3 scripts/portfolio_strategy.py review
python3 scripts/portfolio_strategy.py review --ensure-data   # 强制先刷新数据
python3 scripts/portfolio_strategy.py review --json           # JSON输出
```

**输出内容：**
- 每只持仓的实时价格、市值、盈亏额/百分比
- PE/PB 估值 + 同行业分位（从 MongoDB 聚合）
- 距止损/止盈的距离
- 策略信号（因子评分、趋势、事件、周期）
- 支撑因素 / 风险因素 / 数据缺失清单
- 组合层面：仓位集中度、行业暴露、现金比例、总PnL
- 观察池的当前价格和估值

**数据依赖：** MongoDB (basic_info + daily_quotes) + 实时价格 (腾讯/新浪/东方财富/Yahoo fallback链)

---

### 2. 买入候选筛选 (Buy Plan — 五层漏斗)
从全市场筛选买入候选，5层漏斗逐步收缩。

**入口：**
```bash
python3 scripts/buy_plan.py                          # 默认 top 10
python3 scripts/buy_plan.py --limit 200 --top 15     # 从200只中选top15
python3 scripts/buy_plan.py --industries "半导体,消费电子"  # 限定行业
python3 scripts/buy_plan.py --json                   # JSON输出
```

**五层漏斗逻辑：**
| 层级 | 名称 | 内容 |
|------|------|------|
| Layer 0 | 生存过滤 | 市值>50亿、成交额>5000万、PE 0~200、排除ST、A股主板/创业板/科创板 |
| Layer 1 | 趋势过滤 | 中期趋势向上（MA20/MA60）、质量不差 |
| Layer 2 | 策略匹配 | 动量突破 / 周期共振 / 低位拐点，三选一赋分 |
| Layer 3 | 入场时机 | RSI 不极端、距均线近、短期未过热 |
| Layer 4 | 催化剂 | 资金流、情绪、业绩预期 |
| Layer 5 | 合并排序 | 综合评分 Top N |

**数据依赖：** MongoDB (basic_info + daily_quotes + stock_signals)

---

### 3. 行业轮动雷达 (Sector Radar)
检测行业动量热度和轮动信号，辅助判断"钱在往哪流"。

**入口：**
```bash
python3 scripts/sector_radar.py                           # 热力图 + 雷达
python3 scripts/sector_radar.py --heatmap                 # 仅热力图
python3 scripts/sector_radar.py --radar --top 15          # 仅雷达，top 15
python3 scripts/sector_radar.py --industry "半导体"        # 单行业深入
python3 scripts/sector_radar.py --backfill                # 回填缺失行业分类
```

**两个视角：**
- **动量热力图**：当前哪些行业在风口（涨跌幅、广度、量比排名）
- **轮动雷达**：哪些冷门行业正在苏醒（苏醒分数、资金流入、轮动潜力）

**数据依赖：** MongoDB (daily_quotes 聚合 + basic_info 行业分类)

---

### 4. 事件驱动选股 (Event-Driven Strategy)
从新闻/公告中识别热点股票，结合价值面和技术面多维度验证。

**入口：**
```bash
python3 scripts/event_driven_strategy.py                  # 全市场扫描
python3 scripts/event_driven_strategy.py --code 600941    # 单只股票事件分析
```

**评分维度：** 情绪(30%) + 价值面(35%) + 技术面(35%)，负面情绪自动剔除。

**数据依赖：** AKShare(财联社/东方财富新闻) + MongoDB(价值面/技术面)

---

### 5. 多因子选股 (Pyramid Multifactor Strategy)
三层金字塔框架：行业轮动(20%) → 多因子选股(50%) → 交易执行(30%)。

**入口：**
```bash
python3 scripts/pyramid_multifactor_strategy.py           # 全量运行
```

**因子体系：**
- 价值因子：PE、PB（行业横截面归一化）、股息率（绝对阈值）
- 成长因子：营收增速、利润增速（财务数据新鲜度衰减）
- 质量因子：ROE、毛利率（行业归一化）、负债率、经营现金流/净利润（应计质量）
- 动量因子：1月/3月收益 × 趋势路径质量惩罚

**关键特性：**
- **行业横截面归一化**：PE/PB/ROE/毛利率在同一行业内百分位排名，混合绝对分和相对分（config 中 blend_ratio 可调）
- **因子共线性诊断**：value 高 + quality 低时输出 `factor_conflicts` 警告（潜在价值陷阱）
- **缺失数据处理**：因子无数据时返回 0（非 0.5），`data_quality_score` 准确反映可用因子比例
- **风格权重配置驱动**：权重定义在 `config_complete.yaml` 的 `style_weights` 节，无需改代码
- **动量路径质量**：不只看终点收益，趋势效率低/回撤大的动量信号自动降分
- **Wilder's RSI + 标准 KDJ**：与 TradingView 等平台可交叉验证

**数据依赖：** MongoDB (basic_info + daily_quotes + financial_data) + 同花顺现金流量表 API

---

### 6. 信号聚合 (Strategy Signals)
为单只持仓收集所有维度的信号，统一输出。

**入口（Python API）：**
```python
from strategy_signals import StrategySignalCollector
collector = StrategySignalCollector()
result = collector.collect_for_position(position_dict)
# result["signals"]  -> {factor, trend, event, cycle, ml, microstructure}
# result["supporting_factors"] / result["risk_factors"] / result["missing_data"]
# result["llm_context"] -> 给 LLM 做决策的上下文文本
```

---

### 7. 回测引擎 (Backtest Engine)
用历史预计算信号驱动 buy_plan 回测，验证选股策略的历史表现。

**入口：**
```bash
# 步骤1: 预计算历史信号
python3 scripts/precompute_history.py --start 2026-04-07 --end 2026-06-08

# 步骤2: 运行回测
python3 scripts/backtest_engine.py --start 2026-04-07 --end 2026-06-08 --top 10
python3 scripts/backtest_engine.py --start 2026-04-07 --end 2026-06-08 --stop-loss 0.10 --take-profit 0.25
```

**输出：** 每笔模拟交易的买入/卖出日期、价格、收益率、持仓天数、退出原因；整体胜率、平均收益、最大回撤。

---

### 7b. 因子权重 IC 分析 (Factor Weight Analyzer)
用历史信号中的因子分和未来收益算 Spearman IC，输出数据驱动的权重建议。

**入口：**
```bash
# 分析因子有效性，输出建议权重
python3 scripts/factor_weight_analyzer.py --forward 20

# 对比 IC 建议权重 vs config 当前权重
python3 scripts/factor_weight_analyzer.py --compare
```

**原理：** 对每个历史快照，计算因子分与未来 N 日收益的 Spearman 秩相关系数（IC）。IC 稳定为正 → 因子有效，IC 为负 → 因子反向，IC≈0 → 无预测力。权重 = IC_i / sum(IC)。

**前置条件：** `precompute_history.py --start 2026-03-01` 预计算历史信号（Tushare 日线已覆盖）

---

### 8. 数据导入管道 (Data Import Pipeline)
统一的数据导入入口，支持增量（每日盘后）和全量（每周日）两种模式。

**入口：**
```bash
# 交易日盘后：导入持仓+观察池数据（基础信息+K线+财务）
python3 scripts/data_import_pipeline.py import-portfolio

# 交易日盘后：导入行业资金流
python3 scripts/data_import_pipeline.py import-industry-moneyflow

# 周日晚间：全市场基础信息 + 持仓财务 + 行业资金流
python3 scripts/data_import_pipeline.py import-all

# 全量刷新K线
python3 scripts/data_import_pipeline.py import-universe-quotes --start 2026-01-01

# 回填历史K线（一次性，供回测使用）
python3 scripts/backfill_history_quotes.py --start 2026-01-01 --limit 500

# 计算全市场信号缓存
python3 scripts/data_import_pipeline.py compute-signals

# 回填缺失行业分类
python3 scripts/data_import_pipeline.py backfill-industries
```

---

### 9. 实时行情获取 (Market Data Provider)
统一的行情数据接口，提供多级 fallback 链。

**支持的维度：**
| 方法 | 内容 | 数据源链路 |
|------|------|-----------|
| `get_price()` | 实时价格 | Tencent → Sina → Eastmoney → AKShare → Yahoo |
| `get_quote_snapshot()` | PE/PB/股息/市值 | Yahoo + MongoDB fallback |
| `get_bars()` | 历史K线 | MongoDB → Yahoo |
| `get_trend_signal()` | 趋势+技术指标 | 基于 bars 计算 |
| `get_financial()` | 财务数据 | MongoDB |
| `get_news()` | 个股新闻 | AKShare |
| `get_moneyflow()` | 资金流向 | Tushare → MongoDB cache |
| `get_fx_rate()` | 汇率 | Yahoo → 内置兜底 |
| `get_sector_performance()` | 行业表现 | MongoDB 聚合 |

---

## 数据中心

### MongoDB (本地 localhost:27017)

| 集合 | 内容 |
|------|------|
| `stock_basic_info` | 股票基础信息（代码、名称、行业、市值、PE/PB、最新收盘价） |
| `stock_daily_quotes` | 日线行情（OHLCV、换手率） |
| `stock_financial_data` | 财务数据（ROE、毛利率、负债率、营收/利润增速） |
| `stock_news` | 个股新闻 |
| `stock_hsgt_flow` | 沪深港通资金流 |
| `stock_signals` | 当前信号缓存（因子评分、趋势信号、PE分位、一致预期） |
| `stock_signals_history` | 历史信号快照（按 computed_at 分区，用于回测+IC分析） |
| `stock_dividend` | 股息率历史数据（A股，来自 Tushare daily_basic） |
| `stock_southbound` | 港股南向持仓（来自 Tushare hk_hold） |
| `stock_shortsell` | 港股做空数据（来自 HKEX） |
| `market_moneyflow` | 全市场主力资金流向 |
| `industry_moneyflow` | 行业资金流向（申万行业级） |
| `market_news` | 宏观新闻（Tushare华尔街见闻 / AKShare东方财富） |
| `top_list` | 龙虎榜数据 |

### 外部 API

| API | 用途 |
|-----|------|
| Tushare | A股基础信息、沪深港通资金流、行业分类（6次/天频率限制） |
| AKShare | 港股行情/财务/新闻、A股财务摘要、全市场行情快照 |
| 腾讯 qt.gtimg.cn | 实时价格（首选，最低延迟） |
| 新浪 hq.sinajs.cn | 实时价格（第一备选） |
| 东方财富 push2.eastmoney.com | 实时价格（第二备选） |
| Yahoo Finance | 日线兜底 + 基本面快照(PE/PB/股息) + 汇率 |
| 同花顺 (THS) | A股现金流量表（经营现金流净额） |
| DeepSeek API | 新闻情绪LLM分析（兼容 Anthropic SDK） |
| HKEX | 港股做空数据（免费公开） |

---

## 持仓管理

### 配置文件
- `data/portfolio.yaml` — 真实持仓 + 观察池 + 账户信息
- `config/config_complete.yaml` — 数据源、因子权重、风控参数

### 当前持仓（6只）
| 代码 | 名称 | 风格 | 市场 |
|------|------|------|------|
| 600941 | 中国移动 | 红利防御 | A股 |
| 01810 | 小米集团-W | 科技成长 | 港股 |
| 00700 | 腾讯控股 | 港股核心资产 | 港股 |
| 560860 | 工业有色ETF万家 | 资源周期 | A股 |
| 601006 | 大秦铁路 | 红利防御 | A股 |
| 513180 | 恒生科技ETF | 科技成长 | A股(场内) |

### 定时任务（工作日）
- 10:01 早盘简报
- 13:31 午盘简报
- 14:43 尾盘操作建议
- 16:11 收盘简报

---

## 使用场景速查

| 你想做什么 | 用什么 |
|-----------|--------|
| 看看持仓盈亏和风险 | `portfolio_strategy.py review` |
| 找新的买入标的 | `buy_plan.py --top 15` |
| 判断资金在往哪流 | `sector_radar.py` |
| 挖掘新闻驱动的机会 | `event_driven_strategy.py` |
| 验证选股策略历史表现 | `precompute_history.py` → `backtest_engine.py` |
| 盘后更新数据 | `data_import_pipeline.py import-portfolio` |
| 周末全量刷新 | `data_import_pipeline.py import-all` |
| 查某只股票的因子/趋势信号 | `strategy_signals.py` (Python API) |

---

## 已知限制

1. **因子权重尚未数据驱动**：当前 style_weights 是基于经验的静态配置，`factor_weight_analyzer.py` 已就绪但需要 `precompute_history` 积累 3+ 个月干净数据后才能输出 IC 驱动的权重建议
2. **因子评分全是绝对值**：已部分解决（2026-06-08 加入行业横截面归一化），但 IC 回测验证仍需时间
3. **Tushare** 频率限制（6次/天），部分高频 API 需积分门槛
4. **数据新鲜度**取决于最近一次 `data_import_pipeline.py` 的运行时间
5. **市场覆盖**以 A股为主，港股数据层较薄（依赖东方财富 + 腾讯接口）
6. **不覆盖**：期权、期货、可转债、北交所

# 2026-06-08 质量因子评分优化 + 港股Q1财务数据接入

## 1. 改了什么

**涉及文件：**
- `config/config_complete.yaml` — 质量因子评分阈值从4档改为6档
- `scripts/pyramid_multifactor_strategy.py` — `calculate_quality_score` 重构，新增 `_score_metric` 和 `_freshness_decay` 方法
- `scripts/factor_data_import_service.py` — `_fetch_hk_financial` 替换数据源

**具体变更：**

1. **评分阈值细化（config）**：ROE/毛利率/负债率从4级(`excellent/good/average/low`)扩展为6级(`excellent/very_good/good/average/below_avg`)，对应分值 `1.0/0.85/0.7/0.55/0.4/0.2`，让头部公司之间有区分度

2. **评分逻辑重构（pyramid）**：硬编码if-elif链改为数据驱动的 `_score_metric` 方法，从config读取阈值动态打分。新增 `_freshness_decay` 方法，财务数据超过4个月开始衰减(×0.9)，超过12个月×0.5

3. **港股Q1季报接入（import_service）**：`_fetch_hk_financial` 从 `stock_financial_hk_analysis_indicator_em`(仅年报)切换到 `stock_financial_hk_report_em(indicator='报告期')`(含Q1/Q2/Q3/年报)，直接从利润表/资产负债表提取原始科目计算ROE/毛利率/负债率。ROE根据报告期自动年化(Q1×4, 半年×2等)。新增少数股东权益排除逻辑

## 2. 为什么改

- **质量分缺乏区分度**：腾讯ROE 21%、毛利率56%每次都拿满分0.93，"粘在天花板上"，因为阈值太宽(ROE≥15即满分)
- **数据过期半年**：港股财务数据来自2025年报，Q1季报已发布但未接入，导致所有港股质量分都被额外衰减
- **上游数据源只给年报**：原 `stock_financial_hk_analysis_indicator_em` 不提供季报数据

## 3. 怎么验证

运行 `python3 scripts/portfolio_strategy.py review`：
- 腾讯质量分：0.93 → 0.80（ROE very_good而非excellent，无衰减折扣）
- 小米质量分：0.80 → 0.43（Q1业绩恶化被真实反映，ROE年化7.1%仅below_avg）
- 中国移动/大秦铁路质量分：基本不变（A股数据源未改动）

运行 `python3 scripts/data_import_pipeline.py import-portfolio`：港股财务数据 report_period 变为 20260331，data_source 变为 akshare_stock_financial_hk_report_em

---

## 4. （追加）沉默中性分：缺失数据默认 0.5

**问题**：四个因子（value/growth/quality/momentum）在数据缺失时都返回 0.5（"中性"），`data_quality_score` 把含 `missing_reason` 的非空 dict 也算"可用"。全无数据的股票 composite=0.5、data_quality=1.0，和数据齐全的平庸股票得分一样。

**修复**：
- 因子无数据 → 返回 0 + `details["available"] = False`
- `calculate_composite_score` 仅对 `available` 的因子做加权平均，缺失因子的权重重新分配
- `data_quality_score` 只看 `available` 标志
- 四因子全缺 → composite=0、data_quality=0

## 5. ROE=0 被 `or` 短路当缺失处理

`stock.get('roe') or (financial.get('roe') if financial else None)` — `0 or x` 求值 `x`。ROE 合法为 0%（盈亏平衡点）时被跳过，不参与质量评分。→ 改为显式 `if roe is None` 检查。

## 6. `profit_growth=0` 同样的 `or` 短路

`financial.get('profit_growth') or financial.get('netprofit_yoy')` — 利润增速 0% 时错误 fallback 到 `netprofit_yoy`。→ 改为显式 `is None` 检查。

## 7. 成长因子不做数据新鲜度衰减

`calculate_quality_score` 有 `_freshness_decay(financial)`，成长因子没有。同一份财报，质量分因数据旧而打折，成长分不衰减。→ 成长因子 return 前加入 `_freshness_decay`，与质量因子一致。

---

## 8. 行业横截面归一化：解决绝对阈值跨行业不可比

**问题**：PE=12 的银行（行业均值 PE=5）和 PE=12 的 AI 公司（行业均值 PE=40），value 得分完全相同。银行股 value 分系统性虚高，科技股 value 分系统性虚低。

**方案**：百分位排名 + 混合打分

对 PE/PB（价值因子）和 ROE/毛利率（质量因子）做行业内百分位归一化：

```
relative_score = f(industry_percentile)     # 行业内百分位 → [0.2, 1.0]
blended = 0.5 × relative_score + 0.5 × absolute_score
```

### 新增基础设施（pyramid_multifactor_strategy.py）
- `_build_industry_stats()` — 从 MongoDB 一次性拉取全市场股票，按 `industry_code` 分组，构建 PE/PB/ROE/毛利率排序数组
- `_get_industry_stats()` — 懒加载缓存
- `_get_industry_percentile()` — 二分查找行业内百分位
- `_percentile_to_score()` — 百分位 → [0.2, 1.0] 线性映射
- `_blend_score()` — 混合绝对分和相对分的一行调用

### 修改的打分函数
- `calculate_value_score` — PE、PB 接入行业混合（股息率不变）
- `calculate_quality_score` — ROE、毛利率接入行业混合（资产负债率不变）
- `compute_signal_cache`（data_import_pipeline.py）— 投影加 `industry_code`，构造 stock dict 时传入

### 容错
- 无 `industry_code` → 纯绝对分
- 行业内 peer < 5 只 → 纯绝对分
- PE ≤ 0 → 不参与行业对比
- `blend_ratio: 0` → 完全关闭，与现在一致

### 配置（config_complete.yaml）
```yaml
industry_relative_scoring:
  enabled: true
  min_peers: 5
  blend_ratio: 0.5
  fields:
    value: [pe, pb]
    quality: [roe, gross_margin]
```

---

## 9. 因子共线性诊断：价值-质量分歧标记

**问题**：低 PE/PB 的股票往往也是低 ROE（价值陷阱），value 和 quality 天然负相关，但系统把它们独立加权相加。无共线性检查。

**方案 A（诊断标记）**：`calculate_composite_score` 返回新增 `factor_conflicts` 字段。当 value > 0.6 且 quality <= 0.4 时，标记 `"价值-质量分歧：潜在价值陷阱，需验证利润趋势"`。不修改分数，由下游决定是否降权。

- 不自动扣分的原因：低 quality 也可能是周期底部/一次性减值/turnaround 中，不一定是陷阱
- 真正陷阱的判断需要结合利润趋势，不适合在静态因子层做

---

## 10. 动量因子路径质量：不只看终点，看怎么走过来的

**问题**：`return_1m = (今收-20天前收)/20天前收`，一只平稳涨 15% 的票和一只最后一天拉涨停的票得分相同。高波动高动量的票实际交易中反转概率大得多。

**方案**：原始收益分 × 趋势质量惩罚系数

新增两个静态方法：

- `_momentum_path_quality(quotes, n_days)` — 从行情序列计算三个指标：
  - **趋势效率** = |累计收益| / 每日绝对收益之和。1.0=每天同向移动（完美趋势），0.3=大量来回震荡
  - **最大回撤** — 区间内的峰值到谷底的最大跌幅
  - **日波动率** — 日收益率标准差

- `_trend_quality_multiplier(efficiency, max_dd_pct, total_return_pct)` — 趋势效率 → 惩罚系数：
  - 效率 ≥ 0.45 → ×1.0（不扣分）
  - 效率 0.25~0.45 → ×0.85
  - 效率 0.12~0.25 → ×0.7
  - 效率 < 0.12 → ×0.55
  - 最大回撤 > |累计收益|×1.5 → 额外 ×0.85

**修改的函数**：`calculate_momentum_score` — 1月/3月原始收益分分别乘以各自周期的路径质量惩罚

**验证**（`python3 scripts/portfolio_strategy.py review`）：

| 持仓 | 旧动量 | 新动量 | 降幅 |
|------|--------|--------|------|
| 中国移动 | 0.50 | 0.26 | -48% |
| 腾讯 | 0.40 | 0.21 | -48% |
| 大秦铁路 | 0.30 | 0.20 | -33% |
| 小米 | 0.20 | 0.15 | -25% |

降分原因：当前所有持仓都处于下跌/震荡趋势中，每日路径必然是低效率+高回撤。牛市中平稳上涨的股票效率≈1.0，不受影响。

---

## 11. 质量因子缺现金流维度：OCF/净利润（应计质量）

**问题**：ROE + 毛利率 + 负债率都是应计制指标。高 ROE 但全是应收账款的公司质量分虚高。缺少经营现金流维度。

**方案**：质量因子新增 `ocf_to_net_income`（经营现金流/净利润）子指标。

### 数据源
- **A股**：`ak.stock_financial_cash_ths(symbol, indicator="按报告期")`，提取 `*经营活动产生的现金流量净额`，自动解析"714.47亿"等带单位数值
- **港股**：`ak.stock_financial_hk_report_em(symbol="现金流量表")`，在现有利潤表+资产负债表基础上加第三次调用

### 修改
- `factor_data_import_service.py`：新增 `_fetch_a_cashflow`、`_parse_cf_value` 静态方法；`_fetch_a_financial` 调用并计算 OCF/NI；`_fetch_hk_financial` 加现金流量表查询
- `pyramid_multifactor_strategy.py`：`calculate_quality_score` 新增 `ocf_to_net_income` 子指标
- `config_complete.yaml`：新增 `ocf_to_net_income` 6 级评分阈值

### 验证
- 中国移动 OCF/NI = 2.44（经营现金流 714 亿 / 净利润 293 亿）→ 满分 1.0，盈利含金量极高
- 质量分：0.613（四个子指标：ROE、毛利率、负债率、OCF/NI）

---

## 12. 风格权重从硬编码迁到 config 配置驱动

**问题**：`get_factor_weights` 用 if-elif 链硬编码四因子权重，数值拍脑袋定，改权重需要改代码。

**方案**：权重定义移到 `config_complete.yaml` 的 `style_weights` 节，5 个风格 profile：
- `default` — 均衡型 (0.40/0.20/0.20/0.20)
- `dividend_defensive` — 红利防御：重价值+质量 (0.45/0.10/0.30/0.15)
- `tech_growth` — 科技成长：重成长+动量 (0.15/0.35/0.25/0.25)
- `resource_cyclical` — 资源周期：重动量 (0.25/0.15/0.25/0.35)
- `core_bluechip` — 核心资产/港股：质量优先 (0.25/0.20/0.35/0.20)

每个 profile 附带调权指南注释。匹配逻辑不变，数据来源从代码常量改为 config 字典值。

---

## 13. 因子权重 IC 分析器 (factor_weight_analyzer.py)

**问题**：风格权重是静态配置，无法判断哪个因子在当前市场环境下真正有效。

**方案**：新增 `scripts/factor_weight_analyzer.py`：
- `compute_factor_ic()` — 对每个历史快照日期，算各因子分与未来 N 日收益的 Spearman 秩相关系数
- `suggest_weights()` — 用滚动平均 IC 生成建议权重（IC 为负的因子归零）
- `compare_with_config()` — 对比 IC 建议权重 vs config 当前权重

**前置条件**：`precompute_history.py` 先跑出 ≥10 天历史信号。当前正在后台预计算中。

**用法**：
```bash
python3 scripts/factor_weight_analyzer.py --forward 20
python3 scripts/factor_weight_analyzer.py --compare
```

---

## 12. 新闻情绪 LLM 调用优化：批量 + MongoDB 持久化 + 时间衰减

**问题**：`_event_signal` 对每条新闻单独调一次 LLM（10条新闻=10次调用），6只持仓+4只观察池=最多100次调用。每次 portfolio review 都重新分析同样的新闻。

**方案**：批量调用 + MongoDB 缓存 + 定时预缓存

### 修改的文件
- `scripts/event_driven_strategy.py`
  - `_call_llm` — 兼容 DeepSeek 推理模型的 `ThinkingBlock` + `TextBlock` 双块响应
  - `NewsSentimentAnalyzer` — 新增 `_init_mongo_cache`、`_lookup_mongo_cache`、`_save_mongo_cache`，MongoDB `sentiment_cache` 集合7天 TTL
  - `analyze_sentiment_batch` — 三层缓存：内存 → MongoDB → LLM
  - `get_stock_news` — 返回 `days_old` 字段，取最新N条（默认10条）
- `scripts/market_data_provider.py` — 新增 `analyze_sentiment_batch` 代理方法
- `scripts/strategy_signals.py` — `_event_signal` 改用批量调用 + 时间衰减加权 `exp(-days_old/3)`
- `scripts/data_import_pipeline.py` — 新增 `import-sentiment` 命令，预缓存全组合新闻情绪
- `config/config_complete.yaml` — LLM timeout 从15s调整到30s

### 定时任务
- 工作日 9:03：`python3 scripts/data_import_pipeline.py import-sentiment`
- 70条新闻 → 3批LLM调用，~4分钟跑完
- Portfolio review 和 buy_plan 使用情绪数据时先查 MongoDB 缓存，未命中才调 LLM

### 验证
- `import-sentiment` 首次运行：70条新闻，LLM新分析30条，缓存命中10条
- 再次运行 portfolio review：0 次 LLM 调用，全部命中缓存

---

## 13. 评分基准参照：全市场 + 行业百分位排名

**问题**：`composite_score = 0.7` 对一只消费股是好是差，没有参照系。

**方案**：从 `stock_signals` 全量加载 composite_score，内存排序计算百分位

### 修改的文件
- `scripts/portfolio_strategy.py`
  - 新增 `_load_percentile_ranks()` — 从 MongoDB 拉取2701只股票信号+行业信息，构建全市场和行业内百分位查找表，缓存4小时
  - 新增 `_pct_suffix()` — 格式化输出 `全市场前X% 行业前X%`
  - 持仓和观察池因子输出行追加排名信息

### 输出效果
```
│ 因子: 综合 0.58 全市场前15% 行业前8% (价值...)
```

---

## 14. PE 过滤阈值对齐（200→50）

**问题**：`buy_plan.py` Layer 0 和 `compute_signal_cache` 用 PE<200，但价值因子评分在 PE>25 就给最低分 0.2。PE=90 的股票通过筛选进入评分，但永远拿不到有意义的价值分，浪费计算。

**修复**：三处 PE 上限统一从 200 → 50：
- `buy_plan.py` Layer 0 查询
- `data_import_pipeline.py` 4 处查询（universe、moneyflow、forecast、signal cache）
- `backtest_engine.py` 过滤

`run_multifactor_selection` 已有 `max_pe: 25`（config 驱动），不变。

---

## 15. 成长因子接入分析师一致预期

**问题**：`revenue_growth` + `profit_growth` 都来自最新财报（可能滞后 3-4 个月），缺乏前瞻性。

**方案**：已有 `stock_signals.forecast_min` / `forecast_max`（2647 只股票，Tushare `forecast` API 批量导入），取均值 `(forecast_min + forecast_max) / 2` 作为 `forecast_growth` 子指标接入 `calculate_growth_score`。

**修改**：
- `config_complete.yaml`：`forecast_growth` 4 级阈值
- `pyramid_multifactor_strategy.py`：`calculate_growth_score` 新增 forecast 评分
- `data_import_pipeline.py`：`compute_signal_cache` 批量预取 forecast 数据 + 保护 `$set` 不覆盖已有字段

---

## 16. 6 月/12 月长周期动量（Fama-French 风格）

**问题**：学术上动量因子最显著的是 6-12 月窗口（扣除最近 1 月）。现有只有 1 月/3 月，属于短期反转+动量混合。

**方案**：新增 `return_6m`（120 交易日，扣除近 1 月）和 `return_12m`（250 交易日，扣除近 1 月），含路径质量惩罚。

**修改**：
- `config_complete.yaml`：`return_6m` / `return_12m` 阈值
- `pyramid_multifactor_strategy.py`：`calculate_momentum_score` 新增两个窗口
- K 线拉取量 60→300：`data_import_pipeline.py`、`buy_plan.py`、`market_data_provider.py`

---

## 18. 四个收尾 Bug

### 18a. 路径质量窗口和收益率窗口不匹配
6月/12月动量收益率用 quotes[19:120]（跳过近1月），但路径质量用了 quotes[0:120]（包含近1月）。→ 传入切片 `quotes[19:120]` / `quotes[19:250]`

### 18b. run_multifactor_selection 只取60条K线 + 报告缺展示
- `run_multifactor_selection` 的 `get_recent_quotes(code, 60)` → 300，6m/12m 不再空转
- 文本报告动量行加 6m/12m，成长行加一致预期

### 18c. forecast 数据源问题
`stock_signals.forecast_*` 不在 `basic_info` 也不在 `financial` → 财务导入层 (`_fetch_a_financial` / `_fetch_hk_financial`) 合并 forecast 到 financial doc；`calculate_growth_score` 优先读 financial 再 fallback stock

### 18d. forecast 被财报新鲜度衰减连带
一致预期是前瞻性指标 → 分离计分：仅财报指标做衰减，再与 forecast 平均合并

### 18e. 股息率行业分布排除零分红
`dividend_yield > 0` → `>= 0`，零分红股票纳入全行业百分位参考系

---

## 17. 股息率行业横截面归一化

**问题**：PE/PB 已有行业百分位混合，但股息率还是绝对阈值。银行股 5% 很常见，消费股 3% 就算高股息，行业间不可比。

**修改**：
- `config_complete.yaml`：`industry_relative_scoring.fields.value` 新增 `dividend_yield`；股息率阈值升级为 6 级
- `pyramid_multifactor_strategy.py`：`_build_industry_stats` 新增 `dividend_yield` 数据收集和 `dy_sorted` 排序数组；`calculate_value_score` 股息率改用 `_score_metric` + `_blend_score`（绝对分×0.5 + 行业百分位分×0.5）

**效果**：银行股 5% 股息 → 行业中等（50分位）→ 混合分降低；消费股 3% 股息 → 行业顶级（90分位）→ 混合分拉高。行业间可比。

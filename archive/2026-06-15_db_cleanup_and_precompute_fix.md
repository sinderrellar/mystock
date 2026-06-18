# 2026-06-15 数据库清理 + precompute 性能修复

## 清理

### 删除过期集合
- `market_quotes` (6,475 条) — 旧 TradingAgents 系统遗留，无人读取，最新数据停在 4/28
- `stock_screening_view` (13,376 条) — 同上，无人读取
- `stock_signals_history` (98,936 条) — P0+P1+P2 因子逻辑变更后旧数据作废，清空后全量重算

### 清理 `stock_recommender` 空库
- 确认该库 0 集合，数据全在 `tradingagents` 库。脚本配置指向 `tradingagents`，不影响运行。

## precompute 修复

### 问题1：日期聚合全表扫描
`precompute_history.py` 用 `aggregate($match: {period: "daily"})` 取日期列表，`period` 无索引 → 15M 全表扫 → 卡死。

**修复**：`distinct("trade_date")` 替代，利用 `trade_date_index`，秒出。

### 问题2：历史模式 Tushare PE 分位冗余
`compute_signal_cache` 对每只股票调 `_get_pe_percentile`（Tushare daily_basic API），历史模式 98K 次调用是最大瓶颈。

**修复**：历史模式 (`is_historical=True`) 跳过 Tushare PE 分位查询，使用 basic_info 中已有 PE 值。

## 影响
- 历史回填速度从 ~27 小时降到 ~3-4 小时（8 进程）
- 对回测准确性无实质影响（PE 分位相对稳定）

# 日线数据源统一清理 & MongoDB 去重

日期：2026-06-22

## 背景

项目已在此前将 A 股日线 canonical source 统一为 `tushare`（qfq 前复权），港股为 `akshare_stock_hk_daily`。但 MongoDB `stock_daily_quotes` 中仍残留大量旧数据源的日线记录，且部分代码查询未加 `data_source` 过滤。

## 执行内容

### 1. MongoDB 重复数据删除

删除 `stock_daily_quotes` 中非 canonical 数据源：

| 数据源 | 删除条数 | 原因 |
|--------|---------|------|
| `yahoo` | 728,394 | 旧源，已被 tushare pro_bar 替代 |
| `tencent_kline` | 639,826 | 旧源，已被 tushare pro_bar 替代 |
| `akshare_sina_daily` | 15,142 | 旧源，已被 tushare pro_bar 替代 |
| `akshare_history` | 66 | 历史遗留，从来不被引用 |
| `akshare_hk` | 3 | 孤立数据，港股正源是 akshare_stock_hk_daily |

**清理后**：15,562,106 条，仅两个数据源：
- `tushare`：15,561,688 条（A股，market=A股，currency=CNY，adjust=qfq）
- `akshare_stock_hk_daily`：418 条（港股，market=港股）

### 2. tushare 记录 market 字段统一

旧数据（15,421,884 条）`market=CN` → 更新为 `market=A股`，同时补齐 `currency=CNY`、`adjust=qfq`。

### 3. 代码修复：补齐缺失的 data_source 过滤

以下文件在查询 `daily_quotes` 时缺少 `data_source` 条件，清理后虽无其他源但仍属隐患，已补全：

| 文件 | 行号 | 修改 |
|------|------|------|
| `scripts/event_driven_strategy.py` | 921 | `get_recent_quotes()` 查询添加 `period` + `data_source` 过滤 |
| `scripts/sector_radar.py` | 494 | 量比计算查询添加 `period` + `data_source` 过滤 |
| `scripts/precompute_history.py` | 61 | `resolve_trading_date()` pipeline 添加 `data_source` 过滤 |
| `scripts/precompute_history.py` | 554 | `distinct("trade_date")` 添加 filter |
| `scripts/import_historical_financials.py` | 221 | pipeline 添加 `data_source` 过滤 |

其余组件（`backtest_engine`、`buy_plan`、`portfolio_backtest`、`market_data_provider`、`factor_weight_analyzer`、`data_capability_service`、`portfolio_strategy`、`pyramid_multifactor`、`strategy_signals`）此前已正确使用 `store.quote_source` 过滤，无需修改。

## 验证

- `data_import_pipeline.py data-check`：A股日线 2026-06-22，0 天前 ✅
- 可投池 2,961 只中仅 5 只略微陈旧 ✅
- 所有修改文件语法编译通过 ✅
- 港股 `akshare_stock_hk_daily` 418 条未受影响 ✅

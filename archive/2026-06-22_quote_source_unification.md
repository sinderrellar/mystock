# 2026-06-22 行情源归一到 Tushare qfq

## 背景
- `stock_daily_quotes` 中同时存在 `tushare`、`tencent_kline`、`yahoo`、`akshare_*` 等多源日线。
- 同一 `code + trade_date + period` 下多源并存，旧读路径未指定 `data_source` 时会混源。
- TradingAgents 旧链路写入的 `tushare` 日线来自 `ts.pro_bar(adj="qfq")`，适合作为长期回测 canonical 源。

## 决策
- 生产 A 股日线只保留一个 canonical 源：`data_source="tushare"`。
- 价格口径统一为前复权：`adjust="qfq"`。
- 不再保留多命令、多 fallback、多源回写口子。

## 代码变更
- `MongoFactorDataStore` 内置 `CANONICAL_QUOTE_SOURCE = "tushare"`。
- `get_latest_quote()`、`count_recent_quotes()`、`get_recent_quotes()` 固定读取 Tushare 日线。
- `FactorDataImporter._fetch_a_quotes()` / `_fetch_etf_quotes()` 改为 `ts.pro_bar(adj="qfq")`。
- 删除腾讯 K 线 A 股日线写入口。
- 删除 `sync_quotes_only()`，不再保留 quote-only 腾讯覆盖池导入路径。
- `data_import_pipeline.py` 只保留一个行情导入命令：`import-quotes`。
- `MarketDataProvider.get_bars()` 只读 Mongo canonical 源，不再拉 Yahoo K 线并回写。
- `buy_plan`、`precompute_history`、`portfolio_backtest_engine`、旧 `backtest_engine`、`factor_weight_analyzer` 的直接行情查询固定带 `data_source=tushare`。

## 服务器执行
先补齐 Tushare 数据，再考虑清理旧源：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/data_import_pipeline.py import-quotes --quote-limit 5000 --start-date 2026-04-17 --sleep 0.12
```

补齐后建议检查：

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/data_import_pipeline.py data-check
```

## 未执行
- 本次没有删除 Mongo 里已有的 `tencent_kline` / `yahoo` / `akshare_*` 记录。
- 等 Tushare 补齐并验证通过后，再单独执行数据库清理。

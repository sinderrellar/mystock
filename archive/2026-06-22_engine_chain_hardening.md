# 2026-06-22 工程链路加固

## 改了什么

- `MongoFactorDataStore`
  - 新增 `readonly` / `ensure_indexes` 参数。
  - 读链路可跳过 `_ensure_indexes()`，避免策略运行触发 `createIndexes requires authentication`。
  - `upsert_quotes()` 强制补齐 `symbol=code`，统一 quote 主键语义。
  - 新增 `repair_daily_quote_schema()`，用于显式修复 `stock_daily_quotes` 的字段和旧索引。

- 行情表 schema
  - 当前项目统一用 `code + trade_date + data_source + period` 作为日线唯一键。
  - `symbol` 仅保留为兼容字段，不再作为唯一约束字段。
  - 远端 Mongo 已执行一次迁移：
    - `symbol_missing = 0`
    - `symbol_null = 0`
    - 已删除旧唯一索引 `symbol_date_source_period_unique`

- 新链路 Mongo 初始化
  - `buy_plan`、`portfolio_state_loader`、`market_data_provider`、`portfolio_strategy` 的常见只读路径改为 readonly 初始化。
  - `portfolio_controller.run()` 顶层创建一个 readonly store，并传给 `BuyPlanEngine` 与 `build_live_state()`，减少重复连接和重复初始化。

- 风险/仓位链路
  - 统一 market 字段为 `volatility_index`，`risk_engine` / `execution_engine` 兼容旧 `vol_index`。
  - 组合级 risk 不再使用第一只候选的 entry signal，改为聚合所有候选的 OPEN/ADD 信号压力。
  - live `pf_meta` 不再固定 `max_risk_budget=0.60`，让 `sizing_engine` 按 `market_regime` 选动态预算。

- UI/旧入口
  - 删除 `scripts/dashboard.py`。
  - 保留 `PortfolioStrategy().review()` 作为旧 review / 分析入口，但其常见只读 Mongo 查询也不再触发建索引。

## 验证

```text
python3 -m py_compile ... ok
readonly Mongo init ok
engine sanity ok
BuyPlanEngine(mongo_store=readonly_store) ok
```

远端 `stock_daily_quotes` 当前索引：

```text
code_date_source_period_unique unique=True
code_date_index
code_1_trade_date_1
symbol_index
trade_date_index
symbol_date_index
```

## 后续注意

- 写入/导入类命令仍默认 `ensure_indexes=True`，读策略链路显式 readonly。
- 如果新库或旧库仍带旧唯一索引，可运行：

```bash
python3 scripts/data_import_pipeline.py repair-quote-schema
```

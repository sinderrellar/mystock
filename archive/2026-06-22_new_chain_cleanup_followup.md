# 2026-06-22 新链路清理补线与远端 Mongo 验证

## 改了什么

- 推送分支 `cleanup-new-chain-legacy`
  - 提交：`1de0233 fix: finish new chain cleanup wiring`
  - 范围：`scripts/buy_plan.py`、`scripts/portfolio_controller.py`、`scripts/risk_engine.py`、`scripts/sizing_engine.py`

- `buy_plan.py`
  - 进一步清理旧依赖：移除未使用的 `PyramidMultifactorStrategy`、`BusinessGroupLoader`、`time`。
  - 顶部职责说明明确为 Alpha Rank 候选池：只做候选生成，不做入场、风控、仓位。
  - 输出新增 `market_context`，保留旧 `market` 字段兼容已有调用方。

- `portfolio_controller.py`
  - live 链路继续保留 6/19 分支上的新方向：使用 `portfolio_state_loader.build_live_state()`，不回退到旧 `PortfolioStrategy.review()`。
  - `entry_signal` 透传 `state_snapshot` 给 `risk_engine` 和 `sizing_engine`，让 sizing 能拿到波动率等状态。
  - `trend_signal.available` 不再硬编码为 `True`，改为尊重趋势缓存实际可用状态。
  - 回测补持仓股票时，Mongo 连接改为走统一配置，避免硬编码 `localhost:27017`。
  - live/backtest 都复用 `buy_plan.market_context` 中的 `regime` 和 `breadth_pct`。

- `risk_engine.py`
  - 回撤阈值兼容 `target_dd` 和 `max_drawdown`。
  - 市场状态兼容 `weak/strong`，并继续使用配置里的市场风险参数。

- `sizing_engine.py`
  - `equity` 兼容 `portfolio["total_equity"]`。
  - 保留波动率单位归一化逻辑，支持 `2.5` 表示 2.5%，避免仓位被 100 倍误缩。

## Mongo 处理

- 本机 Mongo 的 `createIndexes requires authentication` 本质是服务端启用了 `--auth`，而项目初始化会创建索引。
- 临时验证过无认证 Mongo 后，`MongoFactorDataStore` 初始化可通过。
- 后续改为使用远端 Mongo，经 SSH 隧道访问：

```bash
ssh -N -L 27018:127.0.0.1:27017 sinderrellar@123.57.128.49 -p 5783
```

- 本地主工作区 `config/config_complete.yaml` 临时指向：

```text
mongodb://127.0.0.1:27018/tradingagents
```

- 该本地隧道配置没有推送到远端分支，远端分支仍保留仓库默认 `mongodb://localhost:27017/tradingagents`。

## 验证

- 远端 Mongo 连接验证通过：

```text
mongo ok tradingagents
```

- 通过远端 Mongo 跑 `buy_plan` 轻量验证：

```text
Layer0 生存过滤: 2081 -> 2021
因子: 50 -> 50 日期=2026-06-18
趋势: 50 只缓存命中
最终候选 3 只
```

- 在 `cleanup-new-chain-legacy` 临时 worktree 中验证：

```text
python3 -m py_compile scripts/buy_plan.py scripts/portfolio_controller.py scripts/sizing_engine.py scripts/risk_engine.py scripts/entry_engine.py
risk/sizing sanity ok
```

## 没推的内容

- `config/config_complete.yaml` 的 `127.0.0.1:27018` 是本机隧道配置，仅留在当前主工作区。
- `todo.md` 是用户已有本地改动，本次未纳入提交。
- 运行产生的 `pyc`、日志、`data/fx_fallback.json` 变更已清理，没有进入提交。

## 下一步

- 继续检查 `portfolio_controller.run()` 全链路在远端 Mongo 下的长跑问题，尤其 Yahoo 回写 `stock_daily_quotes` 时 `symbol: null` 与唯一索引 `symbol_date_source_period_unique` 的字段不一致。
- 旧 `portfolio_strategy.py` 仍应保留为 review/UI 分析入口，但交易链路不应再依赖它。

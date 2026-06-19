# 2026-06-19 新架构链路旧逻辑清理

## 改了什么

- 新增 `scripts/portfolio_state_loader.py`
  - 只负责读取 `data/portfolio.yaml`、解析汇率、获取价格、计算持仓权重/盈亏/现金比例。
  - 输出 `pf_meta`、`pos_list`、`summary`，供新链路 `portfolio_controller` 使用。
  - 不做 review、不做买卖判断、不生成 UI。

- 改造 `scripts/portfolio_controller.py`
  - `run()` 不再 import 和调用 `PortfolioStrategy.review()`。
  - live 链路改为：`buy_plan -> entry -> risk -> sizing -> controller`，组合状态由 `portfolio_state_loader.build_live_state()` 提供。
  - 更新旧注释，避免继续把 `PortfolioStrategy.review()` 当作新链路数据源。

- 清理 `scripts/buy_plan.py`
  - 删除已废弃的 `historical_snapshot/_hist` 分支。
  - `BuyPlanEngine` 统一走 MongoDB + `target_date` 路径。
  - `_get_portfolio_codes()` 改读 `portfolio_state_loader.load_portfolio_yaml()`，不再依赖旧 `PortfolioStrategy`。
  - 顶部旧五层漏斗文案改为 Alpha Rank 候选池定位。

- 改造 `scripts/factor_weight_analyzer.py`
  - IC 分析从旧 `stock_signals_history` 改为读取新表 `stock_factors.trade_date`。
  - 每只股票按其业务组读取对应 `factor_scores`。

- 改造 `scripts/data_import_pipeline.py`
  - 数据健康检查不再检查旧 `stock_signals.computed_at` 信号缓存。
  - 改为检查新架构使用的 `stock_trends` 和 `stock_factors` 最新日期与样本数。

- 文案清理
  - `entry_engine.py`：持仓管理归属改为 `portfolio_controller`。
  - `strategy_signals.py`：标注为旧 review 流程的 legacy support，新交易链路不依赖它。

## 为什么改

6.17 归档确认系统最新目标是多引擎链路：

```text
buy_plan -> entry_engine -> risk_engine -> sizing_engine -> portfolio_controller
```

但代码里仍有旧链路残留：

- `portfolio_controller.run()` 仍调用旧的 `PortfolioStrategy.review()`。
- `buy_plan` 仍有历史快照 `_hist` 分支。
- IC 分析仍读已废弃的 `stock_signals_history`。
- 数据健康检查仍盯旧 `stock_signals` 信号缓存。

这些残留会导致新旧逻辑混跑：表面是新架构，实际部分状态仍由旧 review/旧缓存提供。此次改动的目标是先把新链路的 live 入口从旧巨石中解耦出来。

## 怎么验证

- 已用 bundled Python 做 AST 语法检查，通过文件：
  - `scripts/portfolio_state_loader.py`
  - `scripts/portfolio_controller.py`
  - `scripts/buy_plan.py`
  - `scripts/factor_weight_analyzer.py`
  - `scripts/data_import_pipeline.py`
  - `scripts/entry_engine.py`
  - `scripts/strategy_signals.py`
  - `scripts/portfolio_strategy.py`

- 导入级验证未完成：
  - 当前 Windows 环境无裸 `python` 命令。
  - bundled Python 缺少 `yaml` 模块。
  - 项目 `venv/bin/python` 是类 Unix 布局，在当前 PowerShell 下执行被拒绝。
  - `requirements.txt` 声明了 `PyYAML`，但当前可执行环境不可用。

## 剩余风险 / TODO

- `portfolio_strategy.py` 仍保留为旧 review/展示入口，尚未拆分删除。
- `dashboard.py` 仍调用 `PortfolioStrategy().review()`，应视为旧 dashboard 链路。
- `stock_signals` 仍承载资金流和一致预期字段，不能直接删除；后续应迁移为更明确的集合，例如 `stock_forecasts`、`stock_moneyflow_daily`。
- `market_data_provider.get_industry_moneyflow()` 仍从 `stock_signals.moneyflow_net` 聚合，后续需要随资金流迁移一并改造。
- 新增的 `portfolio_state_loader` 只替代组合状态装配，不包含旧 review 中的动态止损、相关性矩阵、告警展示等分析能力。若这些能力仍需要，应拆成独立只读分析模块，而不是重新依赖 `portfolio_strategy`。

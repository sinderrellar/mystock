# 2026-06-09 portfolio_strategy.py 观察池修复

## 1. 观察池每个 item 新建 PyramidMultifactorStrategy（已修复）
**问题**：观察池循环中每个 item 都 `PyramidMultifactorStrategy(config_path)` 一次，4 个 item = 4 次完整初始化（读 config、连 MongoDB、build industry_stats）。
**修复**：改为 `self.signal_collector._get_pyramid_strategy()` 复用懒加载单例，4 个 item 共享同一实例，industry_stats 只查一次 MongoDB。

## 2. 观察池只取 60 条 K 线（已由 linter 修复）
**问题**：`get_recent_quotes(code, 60)` → 6 月/12 月动量永不生效。
**修复**：→ `get_recent_quotes(code, 260)`，对齐持仓侧和 `data_import_pipeline.py`。

## 3. 观察池传入真实 stock_doc（已由 linter 修复）
**问题**：之前构造 `{"code": code, "close": ...}` 缺少 `industry_code`，导致行业横截面归一化失效。
**修复**：改为 `self.data_provider.mongo.get_basic(code)` 取完整文档，传入 `calculate_composite_score`。

**涉及文件**：`scripts/portfolio_strategy.py`

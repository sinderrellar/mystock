# 回测引擎对齐实盘逻辑 — Bug E + Layer0 filter

## Fix 1: 行业苏醒分 Bug E

`scripts/backtest_engine.py` `_build_snapshot`：

- **删除**：手写的苏醒分计算（`s["close"] - s["close"]` 恒为 0，`pct_sum` 未使用，wake = `breadth × 0.6` 纯广度退化）
- **改为**：直接调用 `SectorRadarEngine._compute_industry_metrics()` → `compute_heatmap()` → `compute_radar()`，复用实盘 5 因子苏醒模型
- **验证**：89 个行业苏醒分有真实区分度（装修装饰 0.437 / 火力发电 0.372 / 路桥 0.356）

## Fix 2: Layer0 filter 对齐

`scripts/buy_plan.py`：
- **新增** `get_layer0_filter()` 模块级函数：提取 Layer 0 MongoDB 查询条件（PE<25, market 过滤, industry_code 检查等），实盘和回测共用

`scripts/buy_plan.py` `_broad_screen`：
- **改为**：调用 `get_layer0_filter()` 替代手写的 query dict

`scripts/backtest_engine.py` `_build_snapshot`：
- **改为**：basic_info 查询使用 `get_layer0_filter()` 的同一套过滤条件
- **删除**：手写的 `pe >= 50` / `mv < 5e9` / `amt < 5000` 过滤（已在 MongoDB 查询中统一执行）
- **效果**：PE < 25（原<50），新增 market/industry_code 过滤，候选池从 930 降到 529（-43%）

## 回测 vs 实盘 对齐状态总结

| 模块 | 对齐方式 | 状态 |
|------|----------|------|
| Layer 0 filter | `get_layer0_filter()` 共享函数 | ✅ |
| trend/factor | `stock_signals_history` 预计算（与实盘同一 `compute_signal_cache`） | ✅ |
| sector wake | `SectorRadarEngine` 方法直接调用 | ✅ |
| L1-L5 漏斗 | `BuyPlanEngine(historical_snapshot=...)` | ✅ |
| cycle_state | 历史模式返回空（合理限制） | — |
| news sentiment | 历史模式跳过（合理限制） | — |

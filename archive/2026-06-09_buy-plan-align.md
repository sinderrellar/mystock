# 2026-06-09 buy_plan.py 修复

## 1. 排序权重失衡 — catalyst 太重
**问题**：`catalyst_score` 上限 4×0.12=0.48，快追上 `strategy_count` 上限 3×0.20=0.60，远超 `composite` 上限 1.0×0.18=0.18。无策略匹配+满催化的票（0.48）能赢过双策略+无催化的票（0.40）。
**修复**：catalyst 0.12→0.08（max 0.32），composite 0.18→0.22（max 0.22），strategy_count 0.20→0.22（max 0.66）。PE 上限 200→50 对齐 filter。动量 0.07→0.05。

## 2. quality/composite 缺失默认 0.5
**问题**：pyramid 策略把缺失数据从 0.5 改成 0+available=False，但 buy_plan 排序仍默认 0.5——没数据的股票在排序中系统性虚高。
**修复**：`quality` 和 `composite` 缺失默认 → 0。

## 3. 信号缓存过期无警告
**问题**：`buy_plan` 读 `stock_signals` 时查 `computed_at >= today-1day`，多天未更新时静默用过期数据。
**修复**：新增缓存新鲜度检查——超过 2 天打警告，无缓存时提示较慢。

**涉及文件**：`scripts/buy_plan.py`

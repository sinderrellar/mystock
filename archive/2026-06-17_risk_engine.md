# 2026-06-17 Risk Engine — 交易系统安全内核

## 职责
只做一件事：判断交易行为是否允许发生。

## 四维风险模型
```
total_risk = 0.40×回撤 + 0.25×波动 + 0.15×集中度 + 0.10×信号压力 + 0.10×市场
```

## Gate（硬闸门）
```
OPEN: total_risk < 0.60 → 允许新开仓
ADD:  total_risk < 0.55 → 允许加仓
NONE: True              → 减仓/不操作永远允许
position_multiplier = max(0.25, 1.0 - total_risk)
```

## 当前组合实测
- 总资产 134万 | 浮亏 -9.8% | 6只持仓
- OPEN: risk=0.55 ✅ 仓位系数 45%
- ADD:  risk=0.51 ✅ 仓位系数 49%
- 回撤风险 0.77（主导）| 集中度 0.15（健康）

## 四引擎架构
```
entry_engine → signal
risk_engine  → gate (allow/deny)
sizing_engine → position size ← 待建
execution_engine → order ← 待建
```

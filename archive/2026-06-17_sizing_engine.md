# 2026-06-17 Sizing Engine — 仓位分配层

## 职责
决定"在允许交易的前提下，每笔交易该下多少资金"。

## v2 升级：从静态权重预算 → 动态风险预算

### 旧模型（v1）
`used = sum(weights)` — 买入后仓位占满，预算永久锁死

### 新模型（v2）
`risk_used = Σ(weight × vol / 2%)` — 低波多拿，高波少拿，波动率降了自动释放

## 公式
```
OPEN: equity × confidence × 1.0(base) × risk_multiplier × penalties
      ÷ max(vol_norm, 0.5) ← 高波少买，低波多买
      上限: min(raw, 20%, remaining_risk_budget)

ADD:  current_weight + current_weight × confidence × 0.4 × risk_multiplier
      ÷ max(vol_norm, 0.5)
      上限: min(add, remaining, 20% - current)
```

## 四层约束
1. 信号驱动: OPEN×1.0, ADD×0.4
2. 风险缩放: × position_multiplier (from risk_engine)
3. 组合预算: risk_used vs max_risk_budget（动态）
4. 回撤/波动/集中度惩罚

## 实测
- 腾讯(vol=0.15): 风险使用 28% → OPEN 0.9%
- 高波股(vol=0.30): 风险使用 28% → OPEN 0.4%（自动缩半）
- 回撤-12%: 自动打七折

## 六引擎完整链
```
buy_plan      → alpha rank
entry_engine  → signal (OPEN/ADD/NONE)
risk_engine   → gate (allow/deny + multiplier)
sizing_engine → risk-parity position size
execution     → order (待建)
feedback loop → portfolio state → risk + sizing
```

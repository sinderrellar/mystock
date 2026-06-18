# 2026-06-17 Execution Engine + Portfolio Controller

## Execution Engine v2
- 订单拆单：根据波动率/流动性/交易规模自动分片（3-12单）
- 执行模式：VWAP/TWAP_SLOW/PASSIVE_LIMIT/ICEBERG/LIMIT_JOIN
- 参与率控制：上限10%，vol↑→participation↓
- 冲击成本：slippage(vol×participation×spread) + impact((size/ADV)²)
- 执行刹车：spread>30bp 或 vol>0.9+大单 → 暂停

## Portfolio Controller（组合大脑）
- 四模式状态机：NORMAL→DEFENSIVE→RISK_OFF→RECOVERY
- 触发：回撤>60%→DEFENSIVE, >80%→RISK_OFF, 恢复<40%→RECOVERY
- 漂移检测：行业过热(>25%)、单票超限(>18%)、亏损聚类(≥3只>10%)
- 组合约束：单票20%、行业30%、最低现金10%
- 动作：TRIM/ROTATE/DELEVERAGE/OPEN/ADD

## 五引擎完整链
```
buy_plan      → alpha rank
entry_engine  → signal (OPEN/ADD/NONE)
risk_engine   → gate (allow + multiplier)
sizing_engine → risk-parity position size
execution     → VWAP/TWAP/LIMIT + slippage + brake
portfolio_controller → mode + drift + rebalance
```

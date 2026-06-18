# 2026-06-17 Entry Engine + 三层架构解耦

## 三层最终形态
```
buy_plan.py            → alpha rank（横截面选股）
entry_engine.py        → signal permission（OPEN/ADD/NONE + state_snapshot）
portfolio_strategy.py  → sole decision maker（position + risk + sizing）
```

## Entry Engine v2
- 从 portfolio_strategy._evaluate_watchlist_buy_timing 拆出独立模块
- 技术信号(60%): trend + MA position + short momentum + reversal + volume
- Alpha加成(40%): momentum/cycle/turnaround，硬约束 technical<0.55 不生效
- 输出 signal {action_type, confidence} + state_snapshot + reason_chain
- 不做 UI、不判断仓位、不替 portfolio 决策

## State Snapshot（统一状态语义）
```python
state_snapshot = {
    trend, momentum_short, reversal, volume,  # 技术
    alpha: {momentum, cycle, turnaround},     # buy_plan因子
    risk: {volatility_20d, atr_pct},          # 风险
    market: {regime, cycle_level}             # 市场
}
```
portfolio 只消费 snapshot，不重复计算信号。

## Signal Model
```python
signal = {
    "action_type": "OPEN" | "ADD" | "NONE",  # 信号类型
    "confidence": 0.62                         # 置信度
}
```
entry 给权限，portfolio 查仓位 + 风险 + 上限，做唯一决策。

## 决策可追溯
reason_chain: ["technical=0.52", "alpha_bonus=+0.10", "market_regime=neutral", ...]

## 删除
- portfolio_strategy._evaluate_watchlist_buy_timing（已搬入 entry_engine）
- entry_engine 不再输出 UI 字符串（label/verdict/details）
- portfolio 不再重复算信号，只消费 state_snapshot

## 验证
- 三项验证通过：alpha微调、不能救垃圾、好技术+好alpha放大
- entry 分布健康：可新开/可加仓/不操作 三级分化

## 相关文件
- scripts/entry_engine.py (新增 ~170行)
- scripts/portfolio_strategy.py (重构 buy_timing + UI 生成)

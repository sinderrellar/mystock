# 2026-06-15 多因子系统优化 P0+P1+P2

## 背景

回测 72 个交易日（2026-03-02 → 2026-06-11），composite_score 排名无预测力（胜率 39-42%，IC ~0）。

根因诊断：
1. P0: value 因子硬编码 pe=None, pb=None → 全 4108 只股票 value=0
2. P1: momentum 因子纯追涨，但 A 股短期呈均值回归（IC = -0.07 ~ -0.09）
3. P2: style_weights 基于错误 IC 校准（6/9 误认为 momentum IC=+0.23）

## 改动

### P0: 修复价值因子 (`scripts/data_import_pipeline.py`)
- 第 417 行：pool 查询加 `pe`, `pb`, `dividend_yield` 字段
- 第 503-504 行：`"pe": None, "pb": None` → `"pe": s.get("pe"), "pb": s.get("pb"), "dividend_yield": s.get("dividend_yield")`

### P1: 动量加入短期反转 (`scripts/pyramid_multifactor_strategy.py`)
- `calculate_momentum_score`: 新增 5 日反转子因子，通过 config 的 `reversal_weight` 控制混合比例
- 新增 `_score_reversal` 静态方法：跌 >8%→1.0 分，涨 >6%→0.1 分（捕捉均值回归）
- 反转默认权重 0.5（config 可调），默认 0.0（向后兼容）

### P2: 权重重校准 (`config/config_complete.yaml`)
- `momentum_factors.reversal_weight: 0.5`
- `style_weights.default`: value 0.15 / growth 0.40 / quality 0.20 / momentum 0.25
- 所有风格 profile 同步更新，momentum 权重从 0.35-0.65 降到 0.15-0.30
- 注释更新为 2026-06-15 回测真实 IC 值

## 验证步骤

1. `python3 scripts/data_import_pipeline.py compute-signals` — 更新实时信号表
2. `python3 scripts/precompute_history.py --start 2026-03-02 --end 2026-06-11 --workers 4` — 回填历史信号
3. 重新跑回测，验证 composite IC 是否 > 0.05

## 影响范围

- 实时信号 `stock_signals` + 历史信号 `stock_signals_history` 全部重写
- buy_plan、portfolio_strategy、sector_radar 的下游排名结果会变化
- 不影响 crontab 调度（crontab 间接触发 compute-signals，新逻辑自动生效）

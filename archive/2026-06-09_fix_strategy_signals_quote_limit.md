# 修复 strategy_signals.py _factor_signal K线数量不足

## 改了什么

`scripts/strategy_signals.py` L92: `get_recent_quotes(code, 60)` → `get_recent_quotes(code, 260)`

同时更新 L93 错误消息中的 "近 60 日行情" → "近 260 日行情"。

## 为什么改

`pyramid_multifactor_strategy.calculate_momentum_score` 的动量窗口需求：
- 近 1 月：20 条
- 近 3 月：60 条
- 近 6 月：130 条
- 近 12 月：260 条

`_factor_signal` 只取 60 条 K 线，6 月/12 月动量永远跳过不计算。每次 portfolio review 中所有持仓的 6 月/12 月动量都静默缺失。

金字塔策略在 `run_multifactor_selection` 已改为 300，但 `_factor_signal` 这个调用点遗漏了。

## 怎么验证

1. 调用 `_factor_signal(code)` 后检查返回的 `momentum_details` 中是否有 `return_6m` 和 `return_12m` 字段（修复前不可能有）
2. 在 portfolio review 中观察日志是否不再出现 "样本不足" 相关 warning

# 2026-06-09 strategy_signals.py 修复

## 1. import math 移到文件顶部
`_event_signal` 方法内 `import math`（行162）→ 移到文件顶部，清理写法。

## 2. _cycle_placeholder 代表资产全部不可用时的降级
**问题**：硬编码的代表资产如果全部不可用（退市/代码变更），返回 `available=False`，整个周期信号失效。
**修复**：全部失败时不再返回 `available=False`，改为 `available=True` + `degraded=True`，用风格标签推断周期位置。

## 3. _factor_signal 不对单个因子做 available 判断
**问题**：`momentum_score <= 0.4` 无条件标记为风险信号。当动量因子 `available=False` 时，pyramid 策略返回 score=0，不代表动量真的弱，只是没有数据。LLM 上下文中会出现"动量因子偏弱: 0.00"的误导信息。
**修复**：增加 `available` 检查——`if momentum_avail and momentum_score <= 0.4`。价值因子同理加 `available` 检查。

## 4. _factor_narratives 动量描述更新
"近1-3个月价格趋势" → "近1-12个月价格趋势（含路径质量）"，反映 6 月/12 月新窗口。

## 5. _factor_evidence LLM 上下文同步新指标
**新增展示**：
- 成长：营收加速(pp)、利润加速(pp)、一致预期增速(%)
- 质量：OCF/NI（经营现金流/净利润）
- 动量：近6月收益(%)、近12月收益(%)
- 行业百分位格式化：PE(行业)、PB(行业)、ROE(行业)、毛利率(行业)
- 数据新鲜度衰减：仅在非 1.0 时展示

**涉及文件**：`scripts/strategy_signals.py`

# 拆除风格周期评分，改用行业动量分

## 改了什么

1. **删 `strategy_signals.py` `_cycle_placeholder()`**（~120行）：不再用3只代表资产手工打分

2. **portfolio_strategy.py 新增 `_ensure_industry_momentum()`**：调用 `SectorRadarEngine` 获取71行业动量分，加行业名映射表和有色金属聚合逻辑

3. **加仓条件第3条**：从 `cycle_score >= 0` 改为 `行业动量分 >= 0.3`（低于"改善"档0.4，高于深度滞后）

4. **决策七维卡**：周期维度 → 行业动量维度

5. **`_build_cycle_summary`** 重写为按行业输出动量分

## 为什么改

- 旧方案用工业有色ETF+黄金ETF+中金黄金代表"资源周期"，黄金拖累和工业金属逻辑错配
- 旧方案三档评分太粗，sector_radar 五因子标准化连续分更细
- 少维护一套评分逻辑

## 怎么验证

- `python3 portfolio_strategy.py review` 正常运行
- 行业动量判断输出6个行业全部有数据（之前周期评分有逻辑硬伤）
- 加仓条件中行业动量不滞后(≥0.3)正确区分了腾讯/恒生科技(互联网0.188)和工业有色(0.350)

## 局限

- sector_radar 仅覆盖A股，港股通过映射表关联A股代理行业，存在近似误差
- sector_radar 数据1天缓存延迟，对低频加仓决策无影响

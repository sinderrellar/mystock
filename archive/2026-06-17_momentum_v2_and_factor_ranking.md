# 2026-06-17 动量 V2 + 周期得分 + 因子排序升级

## 动机
- 旧动量评分（台阶阈值 + 路径质量罚 + 反转惩罚）把 20 日涨 34% 的股票打 0.34 分
- 旧周期共振（三条件硬门槛：广度方向 + PE<中位 + 主力>0）信息损失严重
- 旧 buy_plan 是规则引擎（打勾过门），不是量化选股（因子排序）

## 改动

### 1. 动量 V2：百分位排名（precompute_history.py）
- 每个交易日算全市场 ret5d/ret20d/ret60d 百分位
- V2 = 0.4×Rank(60d) + 0.4×Rank(20d) + 0.2×Rank(5d) + 5×MA多头
- 归一化到 0-1，无硬阈值，连续可比较
- 替换所有 4 组的 factor_scores.momentum

### 2. 周期得分：行业趋势+广度+资金（sector_radar.py + precompute_history.py）
- sector_radar 新增 get_cycle_scores()：
  trend = 0.5×Rank(20d) + 0.3×Rank(60d) + 0.2×Rank(120d)
  breadth = 0.7×Rank(站上MA20占比) + 0.3×Rank(广度变化)
  flow = Rank(净流入/流通市值)
  cycle = 0.45×trend + 0.35×breadth + 0.20×flow
- 三个原始分 + 合成分子分存入 stock_factors

### 3. buy_plan：规则引擎 → 因子排序
- 删除策略 boolean gate，保留标签纯标记
- 周期共振从 cycle>=0.55 改为 cycle>=0.20（仅拒绝极弱行业）
- Layer5 排序公式改为: momentum×0.50 + cycle×0.30 + entry×0.10 + catalyst×0.10

### 4. 回测引擎简化（backtest_engine.py）
- 删除 _switch_to_date（旧 _hist 模式）
- 改用 BuyPlanEngine().run(target_date=date)（新路径）
- 删除 stock_labeler.py + 所有引用

## 数据再生
需运行 --factors-only 更新 momentum + cycle 数据：
```bash
python3 -u precompute_history.py --start 2026-03-02 --end 2026-06-16 --workers 4 --factors-only
```

# portfolio_strategy.review() 完整逻辑图

## ① 加载账户数据
```
_load_portfolio() → portfolio.yaml
  ├─ account: 总资产/现金/币种/汇率/目标(盈利/回撤)
  ├─ positions: 持仓列表(code/成本/数量/买入逻辑)
  └─ watchlist: 观察池(code/目标买入区间/理由)
```

## ② 汇率解析 + 实时定价
```
resolve_fx_rates() → CNY/HKD汇率
data_provider.get_realtime_prices() → 每只票的现价
_build_position_rows() → 计算市值/成本/PnL/权重
watchlist_rows: is_watchlist=True, quantity=0, PnL=0
```

## ③ 逐只深入分析 — 循环 position_rows + watchlist_rows

### 数据补全
```
_ensure_industry_momentum()  → 行业动量缓存
data_provider.get_trend_signal() → RSI/KDJ/MACD/MA/状态
stock_dividend → 股息率
stock_shortsell → 港股做空
stock_factors → alpha_context(momentum/cycle/turnaround) ← v2 Entry Engine
```

### 七大决策模块（每只独立）

**A. _evaluate_position_goal_impact()**
→ 目标贡献：拖累/轻度拖累/贡献
看：PnL vs 最大回撤容忍 | 仓位 vs 上限

**B. _get_industry_peers()**
→ PE/PB 在行业内的分位
→ PE自身历史分位（Tushare）

**C. _suggest_dynamic_stop()**
→ 动态止损价（支撑层+ATR+布林+均线）
输出: stop_price, stop_distance, 支撑层列表

**D. _evaluate_add_conditions() → 5项加仓条件**
① 趋势站上MA20  ② RSI脱离弱势(≥35)
③ 行业动量≥0.3  ④ 仓位<20%  ⑤ 有可用现金

**E. _evaluate_trim_conditions() → 减仓条件**
看：浮盈>30% | RSI>75 | 仓位超限

**F. _build_decision_dimensions() → 七维决策卡**
价格/趋势/估值/资金/周期/情绪/事件 7个维度

**G. _evaluate_watchlist_buy_timing() ← v2 Entry Engine**

```
技术信号层(60%)
  ① 趋势方向(30%): ret20方向
  ② MA位置(20%): 距MA20距离 + MA60确认
  ③ 短期动量(20%): ret5方向 + 是否加速(ret5>ret20)
  ④ 反转信号(20%): RSI温和度 + KDJ J拐点
  ⑤ 量价配合(10%): 放量/缩量信号
  → technical_score (0-1)

Alpha加成(40%)
  momentum/cycle/turnaround → alpha_bonus [-0.1, +0.2]
  硬约束: technical < 0.55 → alpha不生效
  → entry_score → ★评级 → 🟢🟡🔴 标签
```

## ④ 组合级分析
```
_build_cycle_summary()     → 风格暴露(红利/科技/周期/核心)
_build_diagnosis()         → 组合诊断(回撤/现金/集中度)
_build_alerts()            → 触发告警(止损紧迫/回撤超限)
_get_correlation_matrix()  → 持仓相关性
_get_market_breadth()      → 大盘宽度(MA20占比)
_get_sector_heatmap_summary() → 行业动量方向
data_provider.get_market_sentiment() → 北向/南向/两融
data_provider.get_macro_news() → 宏观新闻
```

## ⑤ 格式化输出
```
format_review() → 300行终端输出
  ├─ 账户总览(总资产/现金/浮亏/目标进度)
  ├─ 行业暴露 + 行业动量方向
  ├─ 组合诊断(回撤占用/现金状态/风格倾向)
  ├─ 单只持仓数据卡 × N (每只15-20行)
  │   ├─ 仓位/现价/成本/PnL
  │   ├─ 买入逻辑 + 目标贡献
  │   ├─ PE分位 + 行业参照
  │   ├─ 止损预警 + 动态止损 + 支撑层
  │   ├─ 止盈条件 + 加仓条件
  │   ├─ factor_scores + 趋势状态 + 探底标记
  │   └─ 资金/筹码/财务
  ├─ 观察池数据卡 × N (每只8-12行)
  │   ├─ 买入时机(⭐ + 技术分 + alpha加成) ← v2
  │   ├─ 趋势/MA/RSI/KDJ
  │   └─ PE分位 + 理由 + 目标区间
  └─ 触发告警
```

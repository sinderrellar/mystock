# 2026-06-17 buy_plan v3 架构：因子评分 + 动态权重 + 候选池

## buy_plan 从规则引擎变成因子排序引擎

```
改前：5 层漏斗，每层 boolean gate
  Layer0 → Layer1(策略匹配) → Layer2(趋势过滤) → Layer3(入场) → Layer4(催化) → Layer5(排序)

改后：候选池生成器，不做买卖决策
  Layer0 → _enrich → 因子评分 → 预过滤 → alpha 排序 → Top N
```

## 三个因子分（全部百分位排名，无硬阈值）

### momentum_score（precompute V2）
- 0.4×Rank(ret60d) + 0.4×Rank(ret20d) + 0.2×Rank(ret5d) + 5×MA多头
- 归一化 0-1，线性无台阶，替换旧台阶+路径惩罚+反转

### cycle_score（sector_radar + precompute）
- trend = 0.5×Rank(行业20d) + 0.3×Rank(行业60d) + 0.2×Rank(行业120d)
- breadth = 0.7×Rank(MA20占比) + 0.3×Rank(广度变化)
- flow = Rank(行业净流入/流通市值)
- cycle = 0.45×trend + 0.35×breadth + 0.20×flow
- 三个子分分开存 stock_factors

### turnaround_score（precompute）
- 估值 0.30：行业内PE百分位（越低分越高）
- 价格企稳 0.45：0.40×ret20改善 + 0.30×MA修复 + 0.30×ret5确认
- 行业拐点 0.25：inflection_hits/3
- forecast就绪后自动切四因子：0.25×估值+0.30×盈利+0.25×价格+0.20×拐点

## alpha 排序 + 市场状态自适应

```
alpha = momentum×Wm + cycle×Wc + turnaround×Wt

strong(宽度>60%): Wm=55% Wc=30% Wt=15%    追涨
neutral(35-60%): Wm=45% Wc=35% Wt=20%    均衡
weak(<35%):      Wm=30% Wc=40% Wt=30%    反转+抄底保护(cycle>50%)
```

## 架构定位

```
buy_plan = 候选池生成器（选什么股票值得关注）
  → 只做因子评分 + 排序，不做买卖决策
  
持仓策略 = 买卖决策（什么时候买/卖）
  → 入场时机、止损、止盈
```

## 已删除
- Layer1 策略匹配（boolean gate）
- Layer2 趋势过滤
- Layer3 入场时机过滤（RSI/MA/ret5）
- Layer4 催化剂
- stock_labeler.py
- compute_signal_cache
- stock_signals / stock_signals_history 集合
- _hist 模式 / snapshot 构造

## 回测结果（3/2-3/10，9天）
- 16笔交易，胜率37.5%，均收益+1.22%
- 8笔吃满+30%止盈：新易盛、中际旭创、水晶光电等光模块均抓到
- 比旧架构均收益-6.95%提升约8个百分点

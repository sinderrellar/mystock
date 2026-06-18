# 2026-06-15 标签翻译层 — stock_labeler 接入 review + buy_plan

## 背景
多因子 composite score 把所有信号压成一个数字，丢失了类型差异信息。
同样 RSI 29，周期股是超卖机会，成长股是基本面崩塌。
需要在信号和决策之间加一层"翻译"——按股票类型选解读模板。

## 新增文件

### `scripts/stock_labeler.py` (~200行)
共享模块，portfolio_strategy 和 buy_plan 共用。

**分类**（优先级：红利 > 质量 > 周期 > 成长 > 其他）：
- 🔵 红利型：股息率>3% + growth<0.3 + quality>0.4
- 🟡 质量型：quality>0.6
- 🔴 周期型：value>0.5 + growth<0.4 + 股息率>2%
- 🟢 成长型：growth>0.5 + PE>20
- ⚪ 其他：不满足任一

**信号解读**：每种标签有4个信号灯（便宜/经营/预期/资金），每个灯有阈值和理由。

**综合判断**：3绿0红→左侧买点，3红→陷阱，其他→等等/观望。

## 修改文件

### `scripts/portfolio_strategy.py`
- review() 循环中插入 labeler 调用（~20行），在 strategy_signals 构建完成后
- format_review() 每只持仓追加4行标签解读块，放在买入逻辑后面

### `scripts/buy_plan.py`  
- run() 中 _layer5_rank 之后插入 labeler 调用（~10行）
- format_buy_plan() 每只候选追加1行标签+信号灯

## 验证
- 中国移动 → 🟡 质量型 | 便宜🟢 经营🟡 预期🟢 资金⚪ → 🟡 等一等
- 腾讯控股 → 🟡 质量型 | 便宜⚪ 经营🟢 预期🟢 资金⚪ → 🟡 等一等
- ETF/港股无因子数据 → ⚪ 其他（预期行为）
- buy_plan 候选不足时标签器不触发（预期行为）

## 可调性
所有阈值集中在 StockLabeler 类头部，改数字即生效，无需改逻辑。

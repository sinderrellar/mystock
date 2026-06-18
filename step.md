现在你已经完成了最关键的一轮架构重构：

以前：

规则筛选系统

变成：

多因子候选池系统

后面不要继续无限调因子了。下一步应该进入验证 → 交易层接入 → 回测闭环。

我建议按这个顺序：

第一步：验证三个因子有没有独立价值（现在马上做）

你现在有：

MomentumScore
CycleScore
TurnaroundScore

先不要看最终10只股票。

做因子分析。

每天保存：

股票
日期
momentum
cycle
turnaround
未来5日收益
未来20日收益
未来60日收益

然后看：

1. 分组收益

例如：

Momentum：

Top 10%
Top 20%
中间
Bottom

未来20日平均收益。

期待：

Top10 > Top20 > Middle > Bottom

Cycle 同样。

Turnaround 同样。

如果：

Turnaround Top10%
未来收益 > Bottom

说明这个因子有用。

否则继续改。

第二步：确定 Alpha 合成公式

现在：

alpha =
0.45*momentum
+
0.35*cycle
+
0.20*turnaround

这个只是初始假设。

下一步不是拍脑袋。

测试：

方案A：

45/35/20

方案B：

50/30/20

方案C：

40/40/20

方案D：

动态：

bull:
55/30/15

neutral:
45/35/20

bear:
30/35/35

看哪个未来收益最好。

第三步：重新设计 buy_plan 输出

现在 buy_plan 不应该输出：

买入股票

应该输出：

{
 code:"002273",

 alpha_score:0.82,

 momentum:0.86,
 cycle:0.78,
 turnaround:0.74,

 tags:[
   "momentum",
   "cycle",
   "turnaround"
 ]
}

形成：

Candidate Pool
第四步：建立 Entry Engine（下一大模块）

这是你之前砍掉的 Layer3。

现在它重新回来，但是位置变了。

它不筛股票。

它回答：

候选池里的股票，什么时候买？

例如：

输入：

候选股票

输出：

entry_score

可能结构：

EntryScore

=
0.35 趋势确认
+
0.35 价格位置
+
0.30 风险收益

例如：

趋势：

MA20方向
MA60方向

位置：

距MA20
RSI

风险：

ATR
波动
第五步：Position Manager

这是交易系统核心。

负责：

买入
EntryScore > 阈值
持仓

每天检查：

趋势
行业
个股
卖出

例如：

趋势破坏：

close < MA60

行业退潮：

cycle_score下降

逻辑失效：

turnaround_score下降
第六步：回测

这个必须做。

不要只看模拟盘。

最少：

数据：

过去：

3-5年

每天：

候选池生成
+
排名
+
模拟买入
+
持仓
+
卖出

看：

年化收益
最大回撤
胜率
盈亏比
换手率
你现在的路线图

我觉得：

✅ Step1
Momentum V2

✅ Step2
Cycle Score

✅ Step3
Turnaround V3

⬇️ 现在这里

Step4
因子有效性验证

↓

Step5
Candidate Pool

↓

Step6
Entry Engine

↓

Step7
Position Manager

↓

Step8
完整回测

还有一个建议：

现在不要再增加第四、第五个策略。

你已经有：

趋势因子
行业因子
反转因子

这三个覆盖：

追强
跟风
抄底

已经足够。

下一阶段重点不是“找更多信号”，而是证明：

这些信号能不能稳定赚钱，以及怎么把它们变成交易流程。

你现在已经从“写选股脚本”进入“搭建量化交易框架”的阶段了。下一步最有价值的是跑 因子回测报告。

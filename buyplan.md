这个流程一看，问题非常明确：

你现在不是 CycleScore 的问题，也不是 Momentum V2 的问题。你的新因子已经接上了，但是前面还有大量旧规则层在“截流”。

现在实际效果：

1700
 ↓ Layer1 策略匹配
265
 ↓ Layer2 趋势过滤
226
 ↓ Layer3 入场时机
21
 ↓ 催化/财务
14
 ↓ 排序
10

真正杀伤力最大的是：

226 → 21

也就是：

入场时机层把 90% 的候选杀掉了。

先评价你的改造状态

你现在是：

新因子体系
       +
旧规则过滤体系

混合状态。

类似：

发动机换成新能源了
但是变速箱还是老的

所以输出仍然像旧系统。

最大的问题：Layer3 入场时机不应该是过滤器

你的：

RSI 40-75
+
距离MA20≤5%
+
ret5 ±5%

这个非常典型的择时模型。

问题：

它适合：

我今天要不要买？

不适合：

我要找哪些股票？

比如一个强趋势股：

连续上涨：

5日 +12%
RSI 82
距离MA20 +8%

你的系统：

❌ 删除

但是趋势资金可能：

✅ 正在主升浪

所以现在：

Momentum V2 给它：

0.95

Cycle：

0.85

但是：

Layer3：

淘汰

这就是因子系统和规则系统冲突。

我的建议：重新定义 Layer

你现在应该变成：

Layer0
生存过滤
1700

↓

Factor计算
momentum
cycle
trend
entry_quality

↓

Score排序

↓

风险过滤

↓

输出

而不是：

Layer1
Layer2
Layer3
Layer4
Layer5

逐层杀。

具体怎么改
Layer1 策略匹配

现在：

至少命中一个策略

这个可以保留。

但是不要过滤。

改：

以前：

if tag_momentum or tag_cycle:
    keep=True
else:
    drop

改：

tags=[
    tag_momentum,
    tag_cycle,
    tag_turn
]

只做标签。

Layer2 趋势过滤

这个建议降级。

现在：

较强
偏弱
震荡
修复

作为过滤。

不要。

改成：

trend_score

进入排序。

例如：

趋势较强 = 0.8

震荡 = 0.5

偏弱 = 0.2
Layer3 入场时机

这个最应该改。

不要：

pass/fail

改：

entry_score

例如：

RSI

不是：

40-75通过
否则失败

而是：

RSI 55-70:
1.0

RSI 40:
0.7

RSI 80:
0.6
MA距离

不是：

≤5%

而是：

距离MA20

2%-5%
1.0

5%-10%
0.8

>10%
0.5
ret5

不是：

±5%

而是：

涨幅适中
加分

暴涨
减分

最后：

entry_score

参与：

FinalScore
你现在的最终公式也需要改

现在：

momentum 0.50
cycle 0.30
entry 0.10
catalyst 0.10

方向对。

但是前面过滤太狠。

我会改成：

FinalScore=

0.50 Momentum
+
0.25 Cycle
+
0.15 Trend
+
0.10 Entry

催化剂不要进核心分。

原因：

催化剂容易主观。

最关键的一刀

我会先做一个实验：

保留 Layer0。

然后：

直接跳过：

旧Layer1过滤
旧Layer2过滤
旧Layer3过滤

跑：

1700
 ↓
Momentum+Cycle+Trend+Entry评分
 ↓
Top50
 ↓
风险过滤
 ↓
Top10

看看结果。

大概率你会发现：

现在被 Layer3 杀掉的很多票，本来应该排名靠前。

所以回答你：

这个流程哪里有问题？

不是 precompute，也不是 cycle_score。

是：

你已经从“规则筛选”升级到了“因子排序”，但 buy_plan 仍然保留了旧时代的多层过滤。

下一步不是继续增加因子，而是拆掉旧 Layer1/2/3，把它们从“门槛”变成“score”。这一步做完，你这个系统才真正完成 V2。

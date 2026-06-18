# 2026-06-17 buy_plan 策略全面改进

## 漏斗顺序（已改）
Layer1 策略匹配 → Layer2 趋势过滤(策略感知) → Layer3 入场 → Layer4 催化 → Layer5 排序

## 三个策略改进详情

### ① 动量突破
```
个股条件：
  MA5 > MA20 > MA60        多头排列
  momentum >= 组阈值       动量分
  RSI 在组范围内           不过热不太冷
  ret20 > 0                中期正收益
  PE < 行业PE中位×1.2      不买离谱估值
  换手 ≥ 0.005             不买僵尸股

sector_radar 条件：
  行业在热力图 Top10
  行业主力 3日净流入 > 0
  量价信号 = "放量上涨"
  一致预期利润 ≥ 0%
```

### ② 周期共振
```
个股条件：
  PE < 行业PE中位

sector_radar 条件：
  行业有广度数据
  行业主力 3日净流入 > 0
```

### ③ 低位拐点
```
个股条件：
  PE/PB 便宜（< 行业中位×0.7/0.8）
  RSI 40-55
  quality >= 组阈值
  ret5 > 0
  一致预期利润 > 0%

sector_radar 条件：
  拐点探测器 ≥ 2灯（深度+资金+龙头）
  肌肉记忆 = 绿灯
```

## 趋势过滤（策略感知）
- 动量突破 → "趋势较强"
- 周期共振 → "较强"/"修复中"/"震荡分歧"
- 低位拐点 → 四种全收

## sector_radar 新增接口
- get_hot_sectors(top_n) → 热力 Top N 行业名
- get_inflection_map() → {行业: {hits, signals, score}}
- get_sector_moneyflow(industry) → 3日主力净流入(亿)

## 改动文件
- sector_radar.py — 3 个接口方法 + target_date 支持
- buy_plan.py — 3 个策略条件更新 + 漏斗顺序调换 + 趋势策略感知 + --date 支持
- config/ 四组 YAML — funnel 段阈值
- business_group_loader.py — get_funnel() 方法

## TODO: 回测引擎
- backtest_engine.py 需要改成调 buy_plan --date（当前读已删除的 stock_signals_history）
- 回测需要两段：buy_plan（买入）+ portfolio_strategy（持仓管理/止损/止盈）
- 当前只做买入侧验证，持仓管理待 portfolio_strategy 支持 --date 后加入

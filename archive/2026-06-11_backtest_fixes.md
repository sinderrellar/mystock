# 2026-06-11 回测修复汇总

## Bug 1: 数据重复
- **根因**: stock_daily_quotes 同一天同一股票从 tushare/tencent_kline/akshare_sina_daily 三个源重复写入
- **修复**: 去重脚本，优先保留 tushare，删75万条
- **影响**: .limit(60) 覆盖真实交易日从20天恢复到60天

## Bug 2: 重复建仓
- **根因**: _simulate_trades 无持仓检查
- **修复**: 增加 held_codes 集合，已持仓不开新仓
- **影响**: 314笔→18笔

## Bug 3: buy_price look-ahead
- **根因**: 用 signal_date close 作为买入价
- **修复**: 改用 later[0].open（次日开盘价）
- **影响**: 收益率 +1.80%→+1.87%，差异极小

## Bug 4 (F): composite_score 权重不一致
- **根因**: 回测对全部股票用 style_weights.default 算 composite；实盘按股票风格选不同权重（红利防御/科技成长/资源周期/核心蓝筹各不同）
- **修复**: 直接用预存的 composite_score（与实盘同逻辑），删除 _current_weights
- **影响**: 本次数据下结果未变，但逻辑已修正

## 最终回测结果
| 指标 | 数值 |
|------|------|
| 交易笔数 | 18 |
| 胜率 | 61.1% |
| 平均收益 | +1.87% |
| 中位收益 | +2.63% |
| 盈亏比 | 2.32 |
| 平均持仓 | 68.8天 |

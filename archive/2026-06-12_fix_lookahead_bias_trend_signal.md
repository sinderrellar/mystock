# Fix look-ahead bias — get_trend_signal 预知未来K线

## 改了什么

给 `get_bars` → `get_trend_signal` → `compute_signal_cache` 调用链加 `as_of_date` 参数，确保 precompute 历史回测时只用当时已知的 K 线数据。

### 修改文件

1. **`market_data_provider.py:get_bars`** — 新增 `as_of_date` 参数，传递给 `MongoFactorDataStore.get_recent_quotes(code, limit=days, as_of_date=as_of_date)`，截断未来数据
2. **`market_data_provider.py:get_trend_signal`** — 新增 `as_of_date` 参数，传递给 `get_bars(code, market, days=300, as_of_date=as_of_date)`
3. **`data_import_pipeline.py:compute_signal_cache`** — `get_trend_signal(code, "A股", as_of_date=computed_date)` 传入历史日期

### 为什么这样改

- `get_recent_quotes` 本身已有 `as_of_date` 支持（`{"trade_date": {"$lte": as_of_date}}`），只是 `get_bars` 没传。
- 因子计算（494行）早就正确传了 `as_of_date`，只有趋势信号漏了。
- 所有实时调用方（portfolio_strategy、buy_plan、strategy_signals）不受影响——`as_of_date` 默认 `None`，行为不变。

## 影响

修复后，precompute 历史回测的趋势过滤不再"预知"未来 3 个月走势，胜率将从虚高的 83.3% 回归真实水平。

## 验证

- 两个文件语法编译通过
- 实时调用方不受影响（as_of_date 默认 None）

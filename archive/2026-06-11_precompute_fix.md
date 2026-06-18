# precompute_history 两个问题修复

## 改了什么

### Fix 1: `trade_date` 字段缺失
- **文件**: `scripts/data_import_pipeline.py:517`
- **改动**: `compute_signal_cache` 写入 doc 时新增 `"trade_date": cutoff`
- **影响**: 所有新写入 `stock_signals_history` 的记录会带上 trade_date 字段

### Fix 2: workers=5 MongoDB 断连
- **文件**: `scripts/precompute_history.py:17, 83-95`
- **改动**: `_compute_one_date` 增加指数退避重试（3次，随机jitter），捕获 AutoReconnect 错误
- **影响**: multiprocessing 子进程并发连接时，失败自动错开重试，不再全体一起断连

## 为什么改

1. `trade_date` 是新增字段，虽然现有消费者（backtest_engine、factor_weight_analyzer）都按 `computed_at` 查询不受影响，但补充后方便未来按交易日查询
2. 5 个 workers 同时 spawn 后瞬间 ping MongoDB，连接数打满被踢导致 AutoReconnect。加重试+jitter后自动恢复

## 怎么验证

小范围测试：`python3 scripts/precompute_history.py --start 2026-06-08 --end 2026-06-10 --workers 5`
确认不再报 connection closed，且新记录 trade_date 字段非空。

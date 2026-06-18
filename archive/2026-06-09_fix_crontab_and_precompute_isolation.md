# 修复 crontab arch 导致 compute-signals 三个月失败 + precompute 数据隔离

## 改了什么

### 1. crontab：`arch -x86_64 cd` → `cd && arch -x86_64 python3`

所有 crontab 条目从 `arch -x86_64 cd /path ; python3 script.py` 改为
`cd /path && arch -x86_64 python3 script.py`。

**根因**：`arch` 作用在 `cd`（shell 内建命令）上不生效，导致目录跳转失败，
`python3` 以 arm64 原生运行，加载 x86_64 numpy 崩溃。
`compute-signals` 连续 3 个月静默失败，`stock_signals` 停留在 2026-03-02。

### 2. `compute_signal_cache`：支持目标集合参数

- 新增 `target_collection` 参数（默认 `"stock_signals"`，向后兼容）
- 所有 5 处 `store.db["stock_signals"]` 改为 `store.db[target_collection]`
- 历史模式（`computed_date` 非 None）跳过腾讯 API PE 刷新

### 3. `precompute_history.py`：数据隔离 + close 恢复

- 传 `target_collection="stock_signals_history"`，不再污染 `stock_signals`
- `restore_historical_close` 保存原 close 值到备份 dict
- 新增 `restore_live_close`，precompute 结束后（含异常情况下，finally 块）恢复
- 删除 `archive_signals`（不再需要拷贝）

## 为什么改

- `precompute_history` 直接写 `stock_signals` 并覆盖 `basic_info.close`，
  与每日 `compute-signals` 有竞争条件，且结束后不清理
- crontab `arch -x86_64 cd` bug 让实时数据 3 个月无法更新

## 怎么验证

1. `crontab -l` 确认所有条目为 `cd && arch -x86_64 python3` 模式
2. 运行 `precompute_history --dates 2026-04-01` 后：
   - `stock_signals` 不含 2026-04-01 的记录
   - `stock_signals_history` 含 2026-04-01 的记录
   - `basic_info.close` 保持不变

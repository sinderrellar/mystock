# precompute 接入 crontab 每日调度（补齐「导入→预计算」断链）

## 背景 / 需求（老板 2026-09-07）

排查「precompute 什么情况下会停跑」时发现：**precompute 压根不在 crontab 里，纯手动跑**。crontab 自动跑的一堆 `data_import_pipeline.py` 任务只负责「导入原始数据」（universe-quotes 19:00 写入 daily_quotes），「导入 → 预计算」这一步没人接，得靠人每天手动补 `precompute_history.py`。老板拍板「起来吧」= 加进定时。

## 根因

```
crontab 自动：import-universe-quotes(19:00) → daily_quotes（原始日线）
                                                      ↓ 断口
precompute_history（手动）：读 daily_quotes → stock_factors / stock_trends / stock_meta
```

断口导致：新导入的日线不自动消化成因子/趋势，日线缓存一过期就触发 Yahoo 兜底（见 `archive/2026-09-07_市场情绪指数源修复与交易日新鲜度.md` 的「遗留」）。

## 改了什么

crontab 新增一条（第 42 行）：

```
# 每个交易日 21:00 — 信号预计算（依赖 19:00 universe-quotes 导完当天日线）
0 21 * * 1-5 cd /data2/liuyu20/mystock/scripts && /data2/liuyu20/mystock/venv/bin/python precompute_history.py --date today --workers 8 >> ../logs/precompute.log 2>&1
```

**为什么 21:00、`--workers 8`**：
- 19:00 universe-quotes 先导当天日线，19:30 moneyflow（~30min）、20:00 forecast（~40min）——21:00 时它们都已结束，precompute 读到的是当天完整日线，且不抢 CPU。
- `--workers 8` 对齐今天手动跑成功的参数（8 workers 797s ≈ 13min）。

**为什么用绝对路径**：旧日志里残留大量 `venv/bin/python: No such file or directory`（历史遗留，来自已删除的旧项目 `/data/stock_recommender_system`），当前 11 条 cron 全用绝对路径 `/data2/liuyu20/mystock/venv/bin/python`，新条目照抄，不重蹈相对路径覆辙。

## 怎么验证

1. 备份 crontab 到 `archive/crontab_backup_2026-09-07.txt`（40 行），可回退。
2. `crontab -l` 生效条目 11 → 12，新条目在 42 行，格式与现有条目一致。
3. `/data2/liuyu20/mystock/venv/bin/python --version` = Python 3.9.0（路径有效）。
4. `precompute_history.py --help` 可解析，`--date today` / `--workers 8` 参数齐全（不真跑，避免写库+13min）。

## 影响 / 边界

- 明晚 21:00 起自动跑，首跑结果看 `logs/precompute.log`。
- 若 19:00 universe-quotes 失败（数据源超时）→ 当天 daily_quotes 缺失 → `--date today` 会算不到当天，需次日补。此风险已向老板说明。

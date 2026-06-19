# import-universe-quotes quote-only 优化

## 改了什么

- `scripts/data_import_pipeline.py`
  - `import-universe-quotes` 从完整的 `sync_positions()` 改为行情专用的 `sync_quotes_only()`。
  - 新增 `--workers` 控制并发数，默认 8。
  - 新增 `--quote-sleep` 控制每只股票行情请求后的等待秒数，默认 0。

## 为什么改

`import-universe-quotes` 的职责是补齐全市场日线覆盖，供 `buy_plan` 和 `precompute_history` 使用。

旧链路复用了 `sync_positions()`，每只股票都会额外执行：

- 基础信息刷新
- 财务数据刷新
- 估值兜底
- 固定 `sleep_seconds=0.25`

这对 3000 只股票的行情覆盖来说成本过高，也会让一个日线补数命令承担不该承担的基本面同步职责。

## 关于限频

保留限频能力，但不再把固定 0.25 秒写死在该命令里：

- 默认 `--workers 8 --quote-sleep 0`，适合 Tencent K 线主路径。
- 如果源站不稳或失败率升高，可降为 `--workers 4`，必要时加 `--quote-sleep 0.1`。
- `import-portfolio` / `import-stocks` 仍继续使用 `sync_positions()` 和原 `--sleep`，不改变完整导入语义。

## 怎么验证

建议验证：

```bash
python3 scripts/data_import_pipeline.py config-check
python3 scripts/data_import_pipeline.py import-universe-quotes --quote-limit 20 --workers 4
python3 scripts/data_import_pipeline.py import-universe-quotes --quote-limit 5000 --workers 8
```

如果失败率异常，降速重跑：

```bash
python3 scripts/data_import_pipeline.py import-universe-quotes --quote-limit 5000 --workers 4 --quote-sleep 0.1
```

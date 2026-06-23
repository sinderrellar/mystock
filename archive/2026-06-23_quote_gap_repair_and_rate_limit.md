# 2026-06-23 行情缺口补数与 Tushare 限流

## 背景

- `stock_daily_quotes` 已统一到 A 股 canonical 源 `tushare(qfq)`。
- 远端 Mongo 检查发现 `2026-04-17` 到 `2026-06-22` 的 43 个交易日都有数据，但按当前股票池口径并未完全补齐。
- 最新交易日 `2026-06-22` 仍缺 5 只；区间内每日条数在 `2950 ~ 2961` 之间。
- 直接重跑 `import-quotes` 会触发 Tushare `adj_factor` 频控，报 `200次/分钟` 超限。

## 本次改动

### 1. 给 `import-quotes` 增加滑窗限流

- 新增 `SlidingWindowRateLimiter`
- 默认 `--rate-limit-per-minute 160`
- 在每次 `ts.pro_bar(..., adj="qfq")` 调用前限流

目标：

- 保持当前 `pro_bar` 导入方案不变
- 避免日常补数反复撞 Tushare 分钟级限频

### 2. 给 `import-quotes` 增加缺口定向补数

- 新增 `--repair-missing`
- 先扫描指定 `start_date ~ end_date` 区间内，当前股票池里哪些 code 存在日线缺口
- 仅对缺口股票补数
- 每只股票从最早缺失日开始补，不再对全池做无差别重刷

### 3. 保留当前股票池口径

当前 `import-quotes` 的导入对象不是全市场 5000+ 股票，而是配置股票池：

- `display_market = A股`
- `market in [主板, 创业板, 科创板]`
- `latest_amount >= 5000万`
- `total_mv >= 50亿`

这也是当前覆盖检查里 `2961` 只股票的来源。

## 覆盖检查结论

- 交易日未断档：`2026-04-17 ~ 2026-06-22`
- 但按当前股票池口径，并非全齐
- `2026-06-22` 缺失代码：
  - `001331`
  - `002217`
  - `600717`
  - `600777`
  - `688143`

说明：

- 一部分缺口像是历史补数未覆盖完全
- 一部分缺口像是个股中途停更、停牌或特殊状态，需要补后再复核

## 验证

- `python3 -m py_compile scripts/data_import_pipeline.py`
- `python3 scripts/data_import_pipeline.py --help`

## 推荐补数命令

```bash
python3 scripts/data_import_pipeline.py import-quotes \
  --start-date 2026-04-17 \
  --end-date 2026-06-22 \
  --repair-missing \
  --rate-limit-per-minute 160 \
  --sleep 0
```

如 Tushare 侧仍有偶发抖动，可改为：

```bash
python3 scripts/data_import_pipeline.py import-quotes \
  --start-date 2026-04-17 \
  --end-date 2026-06-22 \
  --repair-missing \
  --rate-limit-per-minute 120 \
  --sleep 0
```

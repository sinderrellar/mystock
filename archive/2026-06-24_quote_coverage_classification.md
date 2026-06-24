# 2026-06-24 行情覆盖分类与口径拆分

## 背景

- A股 `stock_basic_info` 已接近全市场，但 `stock_daily_quotes` 仍明显偏向策略池覆盖。
- 仅看“最新日期 + 条数”会把多种问题混在一起：
  - 历史起点较晚
  - 尾部停更
  - 最新单日缺口
  - 中间零星断点
- 在推进“数据层全量、策略层筛选”前，需要先把覆盖检查口径拆清楚。

## 本次改动

### 1. 精细化缺口补数

`import-quotes --repair-missing` 改为：

- 先扫描缺失交易日
- 将每只股票的缺口切成连续区间
- 按区间逐段补数，而不是只从最早缺口一路补到结束
- 补后回查残留缺口，输出：
  - `unresolved_gap_codes`
  - `unresolved_gap_dates`

### 2. A股覆盖分类

在 `data-check` 中新增 A 股日线覆盖分类，按最近窗口内的缺口行为拆成：

- `late_start`：历史起点晚
- `stale_tail`：尾部停更
- `latest_only`：只缺最新交易日
- `sporadic`：中间零星断点

### 3. 覆盖口径拆分

`data-check` 现在同时输出两套视角：

- `A股日线覆盖分类(策略池)`
- `A股日线覆盖分类(全A股)`

这让“策略可运行性”和“数据层全量程度”不再混淆。

## 验证结果

通过 Mongo 隧道 `127.0.0.1:27018` 验证：

- 最新 `tushare` 日期：`2026-06-23`
- 策略池口径：`2961` 只
- 全A股口径：`5069` 只

`data-check` 实测结果：

- 策略池：
  - `latest_missing=2`
  - `late_start=20`
  - `stale_tail=1`
  - `latest_only=1`
  - `sporadic=49`
- 全A股：
  - `latest_missing=2110`
  - `late_start=20`
  - `stale_tail=1762`
  - `latest_only=1`
  - `sporadic=396`

结论：

- 当前 `daily_quotes` 仍然是“策略池优先”的行情层
- 还不能视为“全A股全量行情库”
- 但策略池内的健康度现在已经可以被更准确地分类和追踪

## 代表性样本

- `stale_tail`：`001331`（停在 `2026-05-27`）
- `latest_only`：`688146`（只缺 `2026-06-23`）
- `late_start`：`001365`、`001393` 等历史起点明显偏晚
- `sporadic`：如 `000793`、`002217`、`300159` 等中间存在零星断点

## 对后续架构的意义

第四步现在可以更明确地推进为：

1. 保持策略池覆盖检查继续服务当前策略链路
2. 逐步引入全A股行情导入能力
3. 最终把“数据层全量”和“策略层筛选”真正解耦

## 验证命令

```bash
python3 -m py_compile scripts/data_import_pipeline.py
```

通过临时改 URI 指向隧道的方式验证：

```bash
python3 scripts/data_import_pipeline.py data-check
```

说明：

- 当前默认配置仍指向 `localhost:27017`
- 本次验证实际使用的是本地 SSH 转发 `127.0.0.1:27018`

# Tushare pro_bar 内部错误输出治理

## 改了什么

- `scripts/data_import_pipeline.py`
  - 新增 `_fetch_tushare_qfq_bar()`，调用 `ts.pro_bar(adj="qfq")` 时捕获其内部 stdout/stderr。
  - 如果 `pro_bar` 打印 `trade_date` 相关内部错误，改为抛出带股票代码上下文的异常，由 `import-quotes` 计入 `failed`。
  - 如果返回 DataFrame 缺少 `trade_date` 列，明确报出 columns，避免裸 pandas 错误污染日志。
  - 当 `pro_bar` 出现 `trade_date` 结构异常时，先用同源 Tushare `daily + adj_factor` 自行合成 qfq 日线，不退回 AKShare。
  - `import-quotes` 输出新增 `fallback` 计数，记录本轮有多少只股票走了 `daily + adj_factor` 兜底。

- `scripts/factor_data_import_service.py`
  - `FactorDataImporter._fetch_tushare_qfq_quotes()` 加同样的 stdout/stderr 捕获与结构校验。
  - `import-portfolio/import-stocks` 也具备同源 `daily + adj_factor` fallback。

## 为什么改

运行：

```bash
python3 scripts/data_import_pipeline.py import-quotes --start-date 2026-04-01
```

时日志中出现：

```text
"None of ['trade_date'] are in the columns"
```

该信息不是项目代码的异常输出，而是 `ts.pro_bar()` 内部在某些标的返回异常结构时直接打印出来的 pandas 错误。原逻辑没有把它计入 `failed`，导致进度显示 `failed=0`，但控制台出现无法定位股票的裸错误。

实盘中已定位到 `688248` 触发该问题。修复后优先使用同源 Tushare fallback 生成 qfq 行情；只有 `daily/adj_factor` 也不可用时才计入失败。

## 怎么验证

```bash
python3 scripts/data_import_pipeline.py import-quotes --quote-limit 20 --start-date 2026-04-01 --sleep 0
```

预期：

- 正常标的继续导入。
- 若 `pro_bar` 返回缺 `trade_date` 的异常结构，会先走 `daily + adj_factor` fallback。
- fallback 也失败时，日志会显示具体股票代码与结构原因，并计入 `failed`。
- 不再出现无股票上下文的裸 `"None of ['trade_date'] are in the columns"`。

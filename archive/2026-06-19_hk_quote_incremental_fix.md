# 港股日线与近期日线跳过逻辑修复

## 改了什么

- `scripts/data_import_pipeline.py`
  - `import-universe-quotes` 的近期日线判断从“自然日今天”改为“北京时间最近 2 天”。
  - A 股候选池为空时不再提前 `return`，仍继续检查并补港股持仓日线。
  - 港股补数不再只写入 `trade_date >= today` 的数据，改为补最近 180 条港股日线。
  - 港股补数复用 `FactorDataImporter._fetch_hk_quotes()` 和统一的 `store.upsert_quotes()`，避免手写字段与数据源不一致。
  - 读取 `portfolio.yaml` 时显式使用 UTF-8。
  - 本文件内所有 YAML 配置/组合读取统一显式使用 UTF-8，避免 Windows 默认 GBK 导致导入命令失败。
  - `import-market-moneyflow` 在 Tushare `moneyflow_mkt_dc` 无权限/失败时，尝试使用 AKShare `stock_market_fund_flow` 作为免费兜底源。
  - `data-check` 的全市场资金流提示改为真实说明：需要运行 `import-market-moneyflow`，Tushare 无权限时会尝试 AKShare/Eastmoney。

## 为什么改

`data-check` 显示港股 01810 / 00700 日线停在 2026-06-05 / 2026-06-04。

根因有两层：

1. `import-universe-quotes` 用自然日 today 判断近期日线。若今天未收盘、休市或数据源暂未更新到今天，即使昨天有 3000 多只 A 股日线，也会被误判为“已有近期日线 0 只”，重复跑全市场。
2. 港股补数只保留 `trade_date >= today` 的记录。若源站最新只到上一交易日，就会导入 0 条，旧数据无法被补上。
3. Windows 下部分导入命令读取中文 YAML 未指定编码，会在 `import-market-moneyflow` 等入口触发 GBK 解码错误。
4. 当前 Tushare token 无 `moneyflow_mkt_dc` 权限，原导入命令无法更新全市场资金流；需要免费兜底源。

## 怎么验证

```bash
python3 scripts/data_import_pipeline.py import-universe-quotes --quote-limit 5000 --workers 8
python3 scripts/data_import_pipeline.py data-check
```

预期：

- A 股若最近 2 天已有日线，会被跳过，不再重复跑 3000 只。
- 港股持仓会补最近 180 条日线。
- `data-check` 中港股日线不再停留在 6 月上旬。

全市场资金流可单独验证：

```bash
python3 scripts/data_import_pipeline.py import-market-moneyflow
```

本地验证中 Tushare 明确返回无权限；AKShare 兜底接口可用性取决于 Eastmoney 网络/代理状态。

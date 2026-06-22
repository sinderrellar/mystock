# 分市场修正 canonical 行情读取口径

## 改了什么

- `scripts/factor_data_import_service.py`
  - `MongoFactorDataStore` 新增 `_quote_source_for_code()`。
  - A 股继续读取 canonical `data_source="tushare"`。
  - 港股读取 `data_source="akshare_stock_hk_daily"`。
  - `get_latest_quote()`、`count_recent_quotes()`、`get_recent_quotes()` 支持 `market` 参数；不传时按代码长度兼容判断。

- `scripts/data_import_pipeline.py`
  - `data-check` 的 A 股日线统计限定为 canonical `tushare`。
  - 港股持仓日线检查显式按 `market="港股"` 读取，避免被 A 股 canonical 规则误杀。
  - `data-check` 的修复建议拆分为 A 股 `import-quotes`、港股持仓 `import-portfolio`、再跑 `precompute_history.py --date today`。

- `scripts/precompute_history.py`
  - 预计算入口的当日价格池限定为 canonical `tushare`，避免混入旧源日线。

## 为什么改

上一轮 `fix: unify quote source to tushare` 的目标是统一 A 股生产日线，但 `MongoFactorDataStore.get_recent_quotes()` 被全局固定为 `data_source="tushare"`。

港股日线仍来自 `akshare_stock_hk_daily`，因此 `data-check` 和持仓港股趋势读取会被误判为“无数据”。这不是港股数据真的不存在，而是读取层把 A 股 canonical 规则套到了港股。

## 怎么验证

```bash
python3 scripts/data_import_pipeline.py config-check
python3 scripts/data_import_pipeline.py data-check
```

预期：

- A 股日线按 `tushare` 统计。
- 港股 01810 / 00700 能读到 `akshare_stock_hk_daily` 的最新日线。
- 预计算只基于 canonical A 股日线，不混旧源。

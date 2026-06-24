# 2026-06-24 港股日线与资金流检查修复

## 背景

`data-check` 显示港股日线停在 `2026-06-18`。进一步检查后，港股并不是所有链路都坏：

- 港股日线：停在 `2026-06-18`
- 港股南向：正常，最新到 `20260623`
- 港股做空：库里已有 `24 JUN 2026`，但旧检查逻辑按字符串排序，误判最新仍是 `29 MAY 2026`

## 本次修复

### 1. 港股日线 fallback

港股日线原来只读写：

- `akshare_stock_hk_daily`

当前环境中 `AKShare stock_hk_daily()` 会触发 `numpy C-extensions failed`，导致港股日线无法刷新。

本次为 `_fetch_hk_quotes()` 增加 Yahoo fallback：

- 主源仍为 `akshare_stock_hk_daily`
- 主源失败或返回空时，退到 Yahoo chart
- fallback 写入 `data_source = yahoo_hk_daily`

同时读取侧改为港股 source 优先级列表：

- `akshare_stock_hk_daily`
- `yahoo_hk_daily`

这样老数据继续可读，新 fallback 数据也能被 `get_recent_quotes(..., market="港股")` 正确读取。

### 2. 港股数据健康检查拆分

`data-check` 对港股从单一“港股日线”扩展为三条检查：

- 港股日线
- 港股南向
- 港股做空

### 3. 港股做空最新日期判定

HKEX 做空数据的 `trade_date` 是文本格式，例如：

- `24 JUN 2026`
- `29 MAY 2026`

旧逻辑按字符串排序会把 `29 MAY 2026` 误判为比 `24 JUN 2026` 更新。

本次新增日期解析 helper：

- `_parse_trade_date_flexible()`
- `_latest_doc_by_trade_date()`

`data-check` 现在按真实日期比较，而不是按字符串比较。

## 数据回补结果

已通过本地 SSH 隧道 `127.0.0.1:27018` 回写远端 Mongo：

### 港股日线

- `01810`：最新 `2026-06-24`，源 `yahoo_hk_daily`
- `00700`：最新 `2026-06-24`，源 `yahoo_hk_daily`

### 港股南向

- `01810`：最新 `20260623`，源 `tushare_hk_hold`
- `00700`：最新 `20260623`，源 `tushare_hk_hold`

### 港股做空

- `01810`：最新 `24 JUN 2026`，源 `hkex`
- `00700`：最新 `24 JUN 2026`，源 `hkex`

## 验证

```bash
python3 -m py_compile scripts/data_import_pipeline.py scripts/factor_data_import_service.py
```

并手工验证：

- `_fetch_hk_quotes()` 可通过 Yahoo fallback 拉到 `2026-06-24`
- `store.get_recent_quotes(code, market="港股")` 可读到 `yahoo_hk_daily`
- `stock_shortsell` 最新日期按真实日期识别为 `24 JUN 2026`

## 后续建议

港股仍建议保持“小范围高可靠”模式：

- 只覆盖持仓和观察池
- 日线、南向、做空三条链路分别检查
- 不做全港股 universe 全量行情

# 全市场资金流（market_moneyflow）补源

日期：2026-09-07

## 背景（老板的问题）

老板问「全市场资金流，所有的数据源都没有吗？」。排查发现 `market_moneyflow` collection 一直空，因为原实现只走 tushare `moneyflow_mkt_dc`，而该接口**无访问权限**（返回「抱歉，您没有接口访问权限」）。

## 结论：有数据源，是东财大盘资金流（akshare）

| 数据源 | 接口 | 权限/状态 |
|--------|------|-----------|
| tushare `moneyflow_mkt_dc` | 大盘资金流 | ❌ 无权限（永久） |
| akshare `stock_market_fund_flow` | 东财大盘资金流（`push2his.eastmoney.com`） | ✅ 可用，已落库 121 条 |
| tushare `moneyflow`（个股） | 个股资金流 | ✅ 有权限，用于 industry_moneyflow 聚合 |

东财接口返回约 120 个交易日的大盘资金流（主力/超大单/大单/中单/小单净额 + 上证收盘/涨跌幅），金额单位**元**。

## 改了什么

`scripts/data_import_pipeline.py::import_market_moneyflow`（原本只走 tushare）：
1. 保留 tushare 优先，失败/无权限降级到 `ak.stock_market_fund_flow()`。
2. akshare 兜底**加重试/退避**（3 次，3s/6s/9s）——见下方「排查过程」。
3. 字段映射：`主力净流入-净额`→`net_amount`、`超大单净流入-净额`→`buy_elg_amount`、`大单净流入-净额`→`buy_lg_amount`、`中单净流入-净额`→`buy_md_amount`、`小单净流入-净额`→`buy_sm_amount`、`上证-收盘价`→`close_sh`、`上证-涨跌幅`→`pct_change_sh`。
4. `net_amount` 等金额**统一存「元」**，与下游 `market_data_provider._get_market_moneyflow` 的 `net / 1e8`（转亿）口径一致。

## 排查过程（为什么之前一直失败）

`ak.stock_market_fund_flow()` 一开始成功（返回 120 行），随后连续多次 `Connection aborted / RemoteDisconnected`。逐步排查：

1. 直接 curl 东财接口（带 Chrome UA + Referer）→ **成功**。
2. Python `urllib.request`（stdlib）→ **成功**。
3. Python `requests`（urllib3）→ 一开始失败，**几分钟后也成功**。
4. 结论：**不是 TLS 指纹、不是 header 差异，而是东财 push2his 接口的临时限流**（首次批量调用后短暂封禁该 IP 的 HTTP 层，几分钟自愈）。故兜底逻辑必须带重试。

## 怎么验证

- 跑 `import_market_moneyflow` → `{'ok': True, 'imported': 121, 'source': 'akshare_stock_market_fund_flow'}`。
- MongoDB 校验：`market_moneyflow` 121 条，`source` 全为 `akshare_stock_market_fund_flow`，`trade_date` 8 位无横线（20260316…20260907 排序正确），`net_amount` 为元（最新 20260907 = 38222614528 元 ≈ 382.2 亿，主力净流入）。
- 下游 `_get_market_moneyflow` 读 MongoDB 走 `net/1e8` 转亿，口径一致 ✅。

## 已知边界（如实告知）

- **tushare 分支的单位隐患**：`import_market_moneyflow` 的 tushare 分支（`moneyflow_mkt_dc`）若未来开通权限，`net_amount` 单位为**万元**，需 `/1e4` 才对齐下游的 `/1e8`。当前无权限不可达，未改（避免无证据改动），已在此记录。
- 东财接口仍是**免费源**，偶尔限流，靠重试兜底；若长期被限，可换 `stock_sector_fund_flow_summary` 等其它东财接口或加代理。
- `market_moneyflow` 只覆盖大盘（上证），不含深市单独序列（akshare 返回里 `secid2=0.399001` 深市字段当前未映射落库，需可后续补）。

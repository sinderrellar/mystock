# 财务数据补齐 + estimated_disclosure 修复

日期：2026-09-07

## 背景

老板要求「财务数据也补齐」。排查发现 `stock_financial_data` 只有 2293 只（来自旧实时导入），3262 只轻量导入股票（universe_light_sina）缺财务（EPS/BPS），导致其 factor 的 value/quality 维度无 PE/PB 输入。

## 排查中发现的第二个 bug（更隐蔽）

补数据时发现旧 2293 只的「latest」财务文档**没有 `estimated_disclosure` 字段**，而 `precompute_history.py` 读财务的查询是：

```python
fin_coll.find_one({"code": code, "estimated_disclosure": {"$lte": target_date}},
                  sort=[("report_period", -1)])
```

MongoDB 里「字段缺失」不匹配 `$lte`，所以这 2293 只的财务**也一直被 precompute 跳过**——也就是说财务口径的 PE/PB 之前对全市场都是失效的（不是只有轻量 3262 只）。

## 改了什么

### 1. `scripts/import_historical_financials.py`（改）

新增 `--only-missing` 参数：只导入 `stock_financial_data` 里还没有财务记录的股票（幂等补缺），跳过已有的，避免全量重导。

### 2. 跑财务补齐

```
python3 import_historical_financials.py --only-missing --start 2025-01-01
→ 完成: 2896/2896 只成功, 0 失败, 共 17361 条财务记录
→ stock_financial_data: 2293 → 5205 只, 19750 条文档
→ 最新 report_period 20260630 覆盖 5204 只（eps>0: 3846, bps>0: 5135, roe: 5204）
```

范围：6/0/3 开头的 A 股 2921 只全补；北交所 920 的 341 只未补（precompute 不算北交所）。

### 3. 补 `estimated_disclosure`（一次性数据修正）

给旧 2293 只 latest 文档补上 `estimated_disclosure`（report_period=20260630 → 2026-08-31，用 `estimate_disclosure_date` 反算）：

```
补齐 estimated_disclosure: 2293 条；残留 0 条
```

### 4. 重跑因子（`--factors-only`）

财务数据变了，重跑因子让 value/quality 维度生效：

```
python3 precompute_history.py --date 2026-09-04 --factors-only
```

## 怎么验证

- 财务覆盖：`stock_financial_data` distinct code = 5205（缺 0 只 6/0/3）。
- `estimated_disclosure` 覆盖：19750/19750 全有。
- 重跑因子后，之前缺财务的 301xxx 等股票 value/quality 维度不再用默认值兜底。

## 已知边界

- 1358 只 eps<=0（亏损股）是正常现象，precompute 会置 pe=None，属预期。
- 北交所 920 未补财务，与 precompute 范围（6/0/3）一致。
- 财务数据来自 akshare `stock_financial_abstract`（sina），与旧实时导入同源、数值一致。

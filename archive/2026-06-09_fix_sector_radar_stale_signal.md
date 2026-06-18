# 2026-06-09 sector_radar.py 修复

## 1. 信号缓存过期只警告不阻止
**问题**：`load_all_industry_data` 发现信号缓存过期只打 `stale_warnings`，照常输出热力图/雷达。今早实际跑出来信号日期是 2026-03-02（过期 3 个月），雷达上显示"化工原料🔥"——这三个月里资源周期早就崩了，用户看到的是严重假象。
**修复**：在 `run()` 中加硬检查——信号日期超过 5 天直接 `return {"error": "..."} `，拒绝输出比给过期数据好。

## 2. 信号查询无过滤无限流
**问题**：`find({"code": {"$in": all_codes}})` 不限 `computed_at`，不限 `batch_size`，A 股 5000 只股票全量拉取，内存和查询耗时不可控。
**修复**：加 `computed_at >= today-2days` 过滤 + `.batch_size(500)` + 字段投影（只取 trend/factor/pe_percentile 等必要字段）。

**涉及文件**：`scripts/sector_radar.py`

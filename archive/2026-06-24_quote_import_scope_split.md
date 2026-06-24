# 2026-06-24 行情导入 scope 拆分

## 背景

- `data-check` 已经明确区分出两套口径：
  - 策略池覆盖
  - 全A股覆盖
- 当前 `stock_basic_info` 已接近全市场，但 `stock_daily_quotes` 仍以策略池为主。
- 在真正推进“数据层全量、策略层筛选”前，需要先让行情导入入口支持两种 scope。

## 本次改动

在 `scripts/data_import_pipeline.py` 中给 `import-quotes` 增加：

- `--scope pool`
- `--scope universe`

默认值仍为：

```bash
--scope pool
```

即保持现有行为不变。

### scope 含义

- `pool`
  - 使用当前策略池过滤条件
  - 即：
    - `display_market = A股`
    - `market in [主板, 创业板, 科创板]`
    - `latest_amount >= min_amount`
    - `total_mv >= min_market_cap`

- `universe`
  - 使用全A股基础 universe
  - 即：
    - `display_market = A股`
    - `market in [主板, 创业板, 科创板]`

## 实现要点

- 新增 `_quote_scope_query(scope, pool)`
- `import_quotes()` 增加 `scope: str = "pool"`
- `repair_missing` 扫描日志和返回结果中加入 `scope`
- CLI 新增：

```bash
--scope {pool,universe}
```

## 目的

这一步不直接把全市场行情导入彻底切过去，而是先把入口做成可切换：

1. 保持当前策略池链路继续稳定工作
2. 允许单独试跑全A股行情导入
3. 为后续真正实现“数据层全量、策略层筛选”打基础

## 验证

```bash
python3 -m py_compile scripts/data_import_pipeline.py
python3 scripts/data_import_pipeline.py --help
```

## 推荐试跑命令

先做小规模 universe 烟雾测试：

```bash
python3 scripts/data_import_pipeline.py import-quotes \
  --scope universe \
  --quote-limit 500 \
  --rate-limit-per-minute 160 \
  --sleep 0
```

若结果稳定，再逐步提升到更大批次。

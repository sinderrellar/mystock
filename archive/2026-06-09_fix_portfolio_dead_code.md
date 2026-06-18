# 修复 portfolio_strategy.py 两个轻度问题

## 改了什么

### 1. `_collect_currencies` 重复 extend（L1763-1764）

同一行 `currencies.extend(...)` 写了两遍。

- 删除重复的 L1764

### 2. `_get_market_breadth` 死代码（L1558-1569）

第一个循环逐条检查 `ma20`/`ma60` 是否存在、计数 `total`，随后 `total = max(len(sigs), 1)` 直接覆盖。前 12 行白算。

- 合并两个循环为一个：直接按 status 计数 `above_ma20`/`above_ma60`
- 用 `len(sigs)` 做分母（`total = max(len(sigs), 1)`）
- 删除 `total = 0` 初始化和无用的 `code` 过滤

## 为什么改

- 重复 extend：无实际影响（set 去重），但每次 review 多遍历一遍 positions
- 死代码：第一个循环的所有工作被下一行覆盖，纯浪费 CPU

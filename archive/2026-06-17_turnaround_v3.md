# 2026-06-17 Turnaround V3 — 因子区分度升级

## V2 问题
- 六个 boolean 条件 AND → 规则引擎，不是因子
- 因子分布集中于 0.4-0.6（std=0.13, 集中度 55%）
- inflection_hits 只有 0/0.33/0.67/1 四档

## V3 升级

### 估值（25%）
- V2：行业内 PE 排名（单维）
- V3：行业内排名 + 自身历史分位（双重交叉）

### 价格企稳（45%）
- V2：ret20改善 + MA修复 + ret5确认
- V3：改善速度(speed_score) + MA修复 + 成交量确认(量比)

### 行业拐点（30%）
- V2：inflection_hits/3（4档离散）
- V3：行业间连续百分位排名

### 标签
- tag_turnaround 阈值从固定 0.7 改为配置化（默认 0.65）
- 所有权重配置化（config_complete.yaml → turnaround 段）

## 目标
- std 从 0.13 提升到 0.20+
- p10 < 0.3, p90 > 0.75
- corr(momentum) 0.2~0.5（健康互补）

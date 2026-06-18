# 2026-06-16 生意模式分组架构

## 变动
- 新建 `config/industry_groups.yaml` — 147 行业 → 4 组映射
- 新建 4 个组 config：`资源周期.yaml` `轻资产成长.yaml` `稳定现金流.yaml` `消费品牌.yaml`
- 新建 `scripts/business_group_loader.py` — 单例共享加载器
- `config_complete.yaml` 精简到 130 行，删除冗余的 value/growth/quality/momentum/stock_labels
- `scripts/stock_labeler.py` 删除 + buy_plan/portfolio_strategy 引用清理

## 四组阈值
- pool: pe_max (资源周期40 / 轻资产成长200 / 稳定现金流15 / 消费品牌60)
- factor_weights: 按组区分
- scoring: PE/PB/growth 阶梯按组区分
- funnel: Layer1/Layer2/Layer3 阈值按组区分
- signals: 左侧信号灯按组区分

## 核心原则
阈值只在 precompute 消费，下游只读表不读 config。

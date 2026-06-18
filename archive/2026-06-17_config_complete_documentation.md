# 2026-06-17 四组 config + 行业分类 + 配置文档化

## 改动
- 4 组 config（资源周期/轻资产成长/稳定现金流/消费品牌）全部中文化注释
- 15 个 scoring 字段补齐（之前缺失：roe/gross_margin/debt_ratio/ocf_to_net_income/forecast_growth/return_1m-12m/reversal_weight）
- industry_groups.yaml：147 行业 → 4 组，含数据库别名映射
- config_complete.yaml 精简到 130 行，只保留基础设施
- reversal_weight bug 修复：从动量 config 移到 scoring 字典

## 评分字段全表
| 因子 | 字段 | 含义 |
|------|------|------|
| 价值 | pe | 市盈率，越低越便宜 |
| 价值 | pb | 市净率，周期股核心 |
| 价值 | dividend_yield | 股息率 |
| 成长 | revenue_growth | 营收增速 |
| 成长 | profit_growth | 利润增速 |
| 成长 | forecast_growth | 一致预期增速 |
| 质量 | roe | 净资产收益率 |
| 质量 | gross_margin | 毛利率 |
| 质量 | debt_ratio | 负债率 |
| 质量 | ocf_to_net_income | 现金流/净利润 |
| 动量 | return_1m/3m/6m/12m | 各窗口收益评分阶梯 |
| 动量 | reversal_weight | 反转权重 |

## 各组关键差异
| 组 | PE max | 价值权重 | 成长权重 | ROE 及格线 |
|----|--------|---------|---------|-----------|
| 资源周期 | 40 | 35% | 20% | 3% |
| 轻资产成长 | 200 | 10% | 45% | 8% |
| 稳定现金流 | 15 | 30% | 10% | 8% |
| 消费品牌 | 60 | 20% | 30% | 8% |

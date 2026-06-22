# buy_plan 推荐留痕

## 改了什么

- `TODO.md`
  - 新增 Strategy Effect Engineering TODO：
    - 推荐留痕
    - Forward test
    - 分层归因
    - 阈值评估

- `scripts/buy_plan.py`
  - 新增 `persist_buy_plan_snapshot()`。
  - CLI 默认把本次 `buy_plan` 推荐写入 MongoDB `buy_plan_snapshots`。
  - 新增 `--no-trace`，用于调试或不想写入样本时关闭留痕。
  - 文本输出末尾显示 `留痕: buy_plan_snapshots/<id>`，方便直接定位样本。
  - 写入字段包括：
    - `generated_at`
    - `run_date`
    - `params`
    - `pipeline`
    - `market_context`
    - `recommendations`
  - 自动创建索引：
    - `generated_at`
    - `run_date`
    - `recommendations.code + run_date`

## 为什么改

策略效果优化的第一步不是继续堆因子，而是让系统能复盘：

- 当天推荐了什么
- 为什么推荐
- 市场环境是什么
- 候选池漏斗如何变化
- 后续 T+1 / T+5 / T+20 表现如何

没有推荐留痕，就无法做稳定的 forward test 和分层归因。

## 怎么验证

```bash
python3 scripts/buy_plan.py --top 3
```

预期：

- 正常输出 buy_plan。
- 输出 JSON 时包含 `trace.collection=buy_plan_snapshots` 和 `trace.id`。
- MongoDB 中新增一条 `buy_plan_snapshots` 文档。

关闭留痕：

```bash
python3 scripts/buy_plan.py --top 3 --no-trace
```

# buy_plan 滚动 forward test

## 改了什么

- `scripts/buy_plan_forward_test.py`
  - 新增独立 forward test 脚本。
  - 读取 MongoDB `buy_plan_snapshots`。
  - 按 canonical A 股行情源计算：
    - T+1 收益
    - T+5 收益
    - T+20 收益
    - 推荐后区间最大回撤
    - 当时 entry signal（OPEN / ADD / NONE）
  - 默认写入 MongoDB `buy_plan_forward_tests`。
  - 支持：
    - `--snapshot-id`
    - `--all`
    - `--limit`
    - `--no-save`
    - `--json`

- `TODO.md`
  - `Recommendation trace` 标记完成。
  - `Forward test` 标记完成。

## 为什么改

策略效果工程需要先形成可复盘闭环：

1. `buy_plan_snapshots` 记录当时推荐了什么、为什么推荐。
2. `buy_plan_forward_tests` 记录推荐后真实表现。

这样后续才能判断：

- 高分票是否真的有 forward return。
- entry signal 是否提高胜率。
- T+1 / T+5 / T+20 哪个周期更适合当前策略。
- 推荐后最大回撤是否可接受。

## 怎么验证

只打印，不写库：

```bash
python3 scripts/buy_plan_forward_test.py --no-save
```

写入最新快照 forward test：

```bash
python3 scripts/buy_plan_forward_test.py
```

刷新最近 N 条：

```bash
python3 scripts/buy_plan_forward_test.py --all --limit 20
```

## 验证结果

本地最新快照为 2026-06-22，当天尚无未来交易日，因此 T+1/T+5/T+20 暂无收益值，但脚本已能：

- 找到快照。
- 找到 entry date。
- 读取 entry close。
- 计算 entry signal。
- 写入 `buy_plan_forward_tests`。

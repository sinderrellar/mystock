# 2026-06-19 全链路回测贡献拆解

## 改了什么

- `portfolio_controller.run_backtest()` 新增 `attribution_mode`：
  - `baseline`：只使用 `buy_plan` TopN，等权买入。
  - `entry`：在 baseline 基础上加入 `entry_engine` 入场许可。
  - `risk`：在 entry 基础上加入 `risk_engine` 闸门和仓位系数。
  - `full`：当前完整链路，使用 `entry + risk + sizing`。

- `portfolio_backtest_engine.py` 新增：
  - `--mode {baseline,entry,risk,full}`：单独跑某一档。
  - `--attribution`：一次跑四档贡献拆解。
  - `--save`：把 attribution 结果保存到 `reports/backtest_attribution_YYYY-MM-DD.json|md`。

## 为什么改

之前只能看到完整链路的总回测结果，无法判断每一层到底有没有贡献：

- `buy_plan` 是否有选股 alpha？
- `entry_engine` 是否提升胜率或降低回撤？
- `risk_engine` 是保护组合还是误杀机会？
- `sizing_engine` 是否改善收益/回撤比？

贡献拆解先解决“模块是否有效”，再决定是否进入参数网格搜索。

## 怎么验证

- 已通过 Python AST 语法检查：
  - `scripts/portfolio_controller.py`
  - `scripts/portfolio_backtest_engine.py`
- 已验证 CLI 参数可正常展示：
  - `python scripts/portfolio_backtest_engine.py --help`

## 使用方式

单独跑完整链路：

```bash
python3 scripts/portfolio_backtest_engine.py --start 2026-03-02 --end 2026-06-17 --mode full
```

跑四档贡献拆解：

```bash
python3 scripts/portfolio_backtest_engine.py --start 2026-03-02 --end 2026-06-17 --attribution
```

保存报告：

```bash
python3 scripts/portfolio_backtest_engine.py --start 2026-03-02 --end 2026-06-17 --attribution --save
```

## 剩余风险 / TODO

- `baseline` 当前是 buy_plan TopN 等权买入并持有到最终或现金不足，不使用 entry 退出；这是刻意设计的纯选股基线。
- `entry/risk/full` 使用现有 entry 恶化退出逻辑，后续可增加统一退出策略对照。
- 交易成本、滑点、冲击成本还未接入组合回测。
- 下一步可在 attribution 稳定后加入 alpha 权重网格和 walk-forward 验证。

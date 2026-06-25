# 2026-06-25 回测胜率口径修正

## 背景

三组组合回测在 `2026-03-24 → 2026-06-24` 窗口出现 100% 胜率。复核后确认核心问题不是行情好时持有有错，而是新版组合回测把期末剩余持仓按最后收盘价强制平仓，并计入交易胜率。

这会把“仍在持有的浮盈”混成“已完成交易胜率”，在 V 型反转或强趋势窗口里显著美化胜率。

## 改动

- `scripts/portfolio_backtest_engine.py`
  - 回测结束不再强制平掉剩余持仓。
  - 期末剩余仓位只做 mark-to-market，并输出到 `open_positions`。
  - `realized_win_rate_pct` 只统计策略自身触发退出的已实现交易。
  - 期末未平仓单独统计：
    - `open_position_count`
    - `open_position_win_rate_pct`
    - `open_positions_unrealized_pnl`
    - `open_positions_unrealized_pnl_pct`
  - 归因报告表格同步改为展示“已实胜率 / 已实交易 / 未平仓 / 浮盈%”。
  - 机械退出参数保留，但默认关闭：
    - `--stop-loss 0`
    - `--take-profit 0`
    - `--max-hold-days 0`
    - `--trailing-stop 0`

## 口径

- 组合总收益仍按净值 mark-to-market 计算，所以趋势持仓带来的收益不会丢。
- 胜率只代表已经完成退出闭环的交易。
- 期末未平仓浮盈/浮亏单独列示，避免再次出现“期末强平胜率 100%”的误读。

## 验证

- `python3 -m py_compile scripts/portfolio_backtest_engine.py`
- 默认口径短窗口验证：
  - `realized_trades=0`
  - `realized_win_rate_pct=null`
  - 期末未平仓单独统计
- 开启 `max_hold_days=1` 验证：
  - 可正常生成 realized trade
  - `exit_distribution` 能记录 `最大持仓 ...` 退出原因
- 极短 attribution 验证：
  - 表格显示 `已实胜率=N/A`
  - 格式正常

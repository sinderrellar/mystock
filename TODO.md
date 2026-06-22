# TODO

## 立即
- [ ] `precompute --factors-only` 重跑（momentum V2 + turnaround V3）
- [ ] 72天完整回测验证

## 下一步
- [ ] Strategy Layer — 多策略并行编排
- [ ] Event Bus — 事件驱动解耦
- [ ] State Store — 状态持久化（回测/复盘/ML）

## Strategy Effect Engineering
- [x] Recommendation trace: persist daily buy_plan recommendations, scores, market context, pipeline, and run params
- [x] Forward test: track T+1 / T+5 / T+20 returns, max drawdown, and entry trigger status
- [ ] Layer attribution: split baseline / entry / risk / sizing / market regime contribution
- [ ] Threshold evaluation: tune entry first, then risk, then sizing to avoid global overfitting

## Strategy Effect Optimization
- [ ] Data leakage audit: verify historical buy_plan only uses data visible as of `--date`, including factor cache, PE/market cap, industry metadata, and financial statement announcement timing
- [ ] Rolling sample validation: run weekly or every-5-trading-day buy_plan forward tests from 2026-03 to 2026-06, tracking T+1/T+5/T+20 mean, median, win rate, max drawdown, and industry concentration
- [ ] Industry concentration control: keep raw `alpha_rank`, add executable `portfolio_rank` with per-industry caps such as Top15 max 5 names, relaxed only in strong regime with high industry cycle score
- [ ] Entry overheating guard: downgrade `OPEN` to `WATCH` or `WAIT_PULLBACK` when recent returns, RSI, MA20 distance, volume spike, or short-term drawdown risk show crowded chasing
- [ ] Regime-aware sizing: use market breadth to control initial exposure; strong regime can buy normally, neutral regime reduces size unless not overheated, weak regime requires pullback confirmation or very small trial size
- [ ] Entry signal calibration: compare `OPEN` / `WATCH` / downgraded candidates in forward tests, then tune entry thresholds before risk and sizing thresholds

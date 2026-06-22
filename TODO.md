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

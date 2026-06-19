# Sizing Volatility Normalization + Attribution Diagnostics

## What changed

- Normalized `sizing_engine` volatility inputs so both `2.35` and `0.0235` are treated as 2.35%.
- Normalized existing-position volatility and `baseline_vol` before risk-budget math.
- Added sizing trace fields:
  - `raw_weight`
  - `target_vol_raw`
  - `target_vol`
  - `vol_norm`
- Added per-day backtest diagnostics from `portfolio_controller.build_backtest_actions()`.
- Aggregated diagnostics in `portfolio_backtest_engine --attribution` summaries and JSON output.
- Added a terminal diagnostics table showing entry/risk/sizing layer pass/block counts.

## Why

The new full-chain attribution showed `full` with almost zero average position. That may be a valid defensive decision, but the prior output could not distinguish:

- entry layer blocking candidates
- risk layer blocking candidates
- sizing layer producing zero or very small target weights
- volatility unit mismatch shrinking weights by about 100x

`stock_trends.volatility_20d` is produced as a percentage number in existing code, while sizing budget math expects decimal volatility. Normalizing this inside sizing avoids a silent unit mismatch across live and backtest paths.

## How to verify

Run syntax checks:

```bash
python3 -m py_compile scripts/sizing_engine.py scripts/portfolio_controller.py scripts/portfolio_backtest_engine.py
```

Run a short attribution window:

```bash
python3 scripts/portfolio_backtest_engine.py \
  --start 2026-06-17 --end 2026-06-18 \
  --attribution --json
```

Expected shape:

- `summaries[*].diagnostics` exists.
- top-level `diagnostics` exists.
- terminal output without `--json` includes `Layer diagnostics`.
- `full` should no longer be shrunk by percentage/decimal volatility mismatch.

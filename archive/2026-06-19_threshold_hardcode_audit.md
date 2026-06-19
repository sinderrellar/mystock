# Threshold Hardcode Audit

## MongoDB verification

Using the live local MongoDB (`tradingagents`) on 2026-06-19:

| Pool | Count |
|---|---:|
| A-share active coverage pool, no PE filter | 3043 |
| Old `0 < PE < 50` pool | 1127 |
| `PE <= 0` inside coverage pool | 809 |
| `PE >= 50` inside coverage pool | 1107 |
| Latest daily quote date | 2026-06-18 |
| Codes with latest daily quotes | 571 |

Conclusion: the old quote import filter capped the daily quote universe before alpha/risk logic could see many loss-making, high-growth, high-valuation, and turnaround names.

## Fixed now

`scripts/data_import_pipeline.py`

- `import_universe_quotes()` no longer applies `pe > 0 and pe < 50`.
- Quote coverage uses market, turnover amount, market cap, and missing recent quotes only.
- Default `import-universe-quotes` limit is now `5000`.
- Result includes `coverage_pool_count`.

## Remaining hardcoded thresholds

### Data import layer

`scripts/data_import_pipeline.py`

- `import_stock_moneyflow()` still uses:
  - `latest_amount >= 5000`
  - `total_mv >= 5_000_000_000`
  - `0 < pe < 50`
- `import_stock_forecast()` uses the same PE-capped query.
- These are lower priority than quote coverage because moneyflow/forecast APIs are rate limited, but they still bias optional signals toward profitable low/mid PE names.
- `quote_limit=180`, `sleep_seconds=0.6`, `sleep_ms=600/800`, `limit=3000` are operational defaults, not alpha rules.

### Buy plan layer

`scripts/buy_plan.py`

- Pool query reads `min_amount` and `min_market_cap` from config, but still defaults `pe_max=200`.
- Pool query still requires `pe > 0`, so loss-making companies are excluded from candidate generation even if quotes exist.
- Price sanity filter: `price <= 0 or price > 200`.
- Cache freshness warning: `cache_age >= 2`.
- Market halt is configurable through `market_breadth.halt_threshold`.
- Market regime breadth thresholds and alpha weights are configurable.
- Remaining hardcoded strategy thresholds:
  - `cycle < 0.20` hard block
  - weak-regime `cycle < weak_min_cycle` is configurable
  - `turnaround >= 0.7` tag
  - held-position penalty multiplier `0.5`
  - defaults `top_n=10`, `initial_limit=500`, `enrich_limit=200`

### Entry engine

`scripts/entry_engine.py`

Most entry thresholds are hardcoded:

- 20d return: `>0`, `>-5`, else weak
- MA20 distance: `>-3`
- 5d return: `>0`, `>-3`
- RSI: `40-60`, `<30`, `>75`
- KDJ J: `0-10`, `<0`
- volume score: `0.7` for volume expansion, `0.3` for contraction
- technical weights: `0.30/0.20/0.20/0.20/0.10`
- alpha blend: `0.40/0.35/0.25`
- alpha applies only when `technical_score >= 0.55`
- action thresholds: `OPEN >= 0.65`, `ADD >= 0.45`
- cycle regime thresholds: `0.6/0.4`
- high-vol regime: `volatility_20d > 3.0`

This is the next best place to move thresholds into config because it directly controls `entry` attribution.

### Risk engine

`scripts/risk_engine.py`

Risk is almost fully hardcoded:

- target drawdown default `0.20`
- volatility defaults and cutoffs: `0.5`, `>0.7`, `>0.4`
- signal risk: `ADD = confidence * 0.6`
- market regime adjustments: cooling `+0.2`, improving `-0.1`
- component weights: `0.40/0.25/0.15/0.10/0.10`
- regime cutoffs: `<0.45`, `<0.65`
- gates: `OPEN <0.60`, `ADD <0.55`
- position multiplier floor: `0.25`
- reason thresholds: `0.5/0.7`

These should be config-driven before tuning risk attribution.

### Sizing engine

`scripts/sizing_engine.py`

Partly configurable, but several important constants remain hardcoded:

- drawdown thresholds `0.10/0.15` are duplicated instead of using config values
- drawdown penalties `0.7/0.5`
- high-vol penalty `vol_20d > 0.25`, multiplier `0.8`
- concentration penalty `max_weight > 0.15`, multiplier `0.6`
- open sizing base cap `0.20`
- single-name cap `0.20`
- add sizing multiplier `0.5`
- volatility normalization floor `0.5`

This should be consolidated with `pyramid_middle_layer.position_sizing`.

### Portfolio controller

`scripts/portfolio_controller.py`

- max drawdown default `0.20`
- mode thresholds:
  - defensive/risk-off drawdown pressure `0.6/0.8`
  - total risk `0.65/0.80`
  - recovery thresholds `0.3/0.4/0.5`
- constraints:
  - max single `0.20`
  - max sector `0.30`
  - min cash `0.10/0.25`
  - max position count `12/8`
- baseline attribution weight `min(0.10, budget/top_n)`
- close rule `entry_score < 0.35`
- drift warnings:
  - sector `>0.25`
  - single name `>0.18`
  - losing positions `>=3` and pnl `<-0.10`

### Backtest engine

`scripts/portfolio_backtest_engine.py`

- defaults: budget `0.60`, baseline vol `0.025`, top `10`, cash `1_000_000`
- execution assumes T+1 next open
- A-share lot size fixed at `100`
- no transaction cost/slippage model yet
- initial position volatility defaults to `0.02`

These are acceptable as simulator assumptions, but should become backtest config before parameter sweeps.

### Sector radar and precompute

`scripts/sector_radar.py`

- RSI buckets, wake-up score weights, volume divergence thresholds, depth thresholds, and PE median clipping are mostly hardcoded.
- Some top-level radar thresholds are configurable, but not enough for systematic tuning.

`scripts/precompute_history.py`

- minimum quote sample `50`
- excludes ST names
- PE clipping `0 < pe < 500`
- several turnaround formula weights are hardcoded even though top-level turnaround weights are config-driven.

## Suggested migration order

1. Data coverage: done for `import_universe_quotes`.
2. New-chain decision thresholds:
   - `entry_engine`
   - `risk_engine`
   - remaining `sizing_engine`
   - `portfolio_controller`
3. Candidate pool policy:
   - decide whether buy_plan should keep `PE > 0` or allow loss-making stocks with missing/penalized value scores.
4. Rate-limited optional data:
   - moneyflow and forecast import pools should get their own config and batching policy.
5. Sector/precompute formulas:
   - move only after the core chain attribution is stable.

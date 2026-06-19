# Universe Quote Coverage Filter

## What changed

- `import_universe_quotes()` no longer filters A-share quote coverage by PE.
- Quote coverage now uses only:
  - market allowlist
  - `latest_amount >= stock_pool.min_amount`
  - `total_mv >= stock_pool.min_market_cap`
  - missing recent daily quotes
- The function reads thresholds from `config_complete.yaml` through `_get_pool_filters()`.
- Default `import-universe-quotes` limit is now `5000` instead of the old small default path.
- The import result now includes `coverage_pool_count`.

## Why

Daily quote coverage is infrastructure data. It should not be capped by valuation rules such as `pe > 0 and pe < 50`.

The old filter excluded:

- loss-making companies (`PE <= 0`)
- high-growth/high-valuation companies (`PE >= 50`)
- turnaround and cycle candidates before the strategy layer could score them

PE-based eligibility belongs in downstream factor/strategy ranking, not in the market data coverage layer.

## How to verify

Run:

```bash
python3 scripts/data_import_pipeline.py import-universe-quotes
```

or explicitly:

```bash
python3 scripts/data_import_pipeline.py import-universe-quotes --quote-limit 5000
```

Expected behavior:

- output prints `行情覆盖池: <N> 只`
- returned result includes `coverage_pool_count`
- coverage pool should be materially larger than the old PE-capped pool

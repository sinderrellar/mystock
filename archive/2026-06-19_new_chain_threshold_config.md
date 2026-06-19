# New Chain Threshold Config Migration

## What changed

- Added `scripts/engine_config.py` as a shared config loader for new-chain engines.
- Added config sections under `pyramid_middle_layer`:
  - `entry_engine`
  - `risk_engine`
  - expanded `position_sizing`
  - `portfolio_controller`
- Updated config validation to require the new sections.
- Migrated hardcoded thresholds from:
  - `scripts/entry_engine.py`
  - `scripts/risk_engine.py`
  - `scripts/sizing_engine.py`
  - `scripts/portfolio_controller.py`

## Why

The attribution engine is now useful enough that threshold tuning should happen through config, not scattered constants. This keeps experiments reproducible and makes future parameter sweeps possible.

Default values were copied from existing code so this migration should not intentionally change behavior.

## How to verify

```bash
python3 scripts/data_import_pipeline.py config-check
python3 -m py_compile \
  scripts/engine_config.py \
  scripts/entry_engine.py \
  scripts/risk_engine.py \
  scripts/sizing_engine.py \
  scripts/portfolio_controller.py
```

Then run a short attribution check:

```bash
python3 scripts/portfolio_backtest_engine.py \
  --start 2026-06-17 --end 2026-06-18 \
  --attribution --json
```

Expected behavior:

- config-check returns `CONFIG_OK`
- attribution output still includes layer diagnostics
- tuning can now be done by editing `config_complete.yaml`, not engine code

# Config Validation Guard

## What changed

- Added `ConfigError` in `scripts/factor_data_import_service.py`.
- Wrapped YAML loading errors with clearer file/line context.
- Added `validate_system_config()` for required import/backtest config blocks:
  - `data_sources.mongodb`
  - `pyramid_middle_layer.market_regime.alpha_weights`
  - `pyramid_middle_layer.position_sizing`
  - `pyramid_middle_layer.turnaround`
  - `pyramid_middle_layer.sector_radar`
  - `pyramid_bottom_layer.position_sizing`
- `_load_mongodb_config()` now validates the full system config before returning MongoDB settings.
- Added `config-check` command to:
  - `scripts/data_import_pipeline.py`
  - `scripts/factor_data_import_service.py`
- Removed tracked `scripts/__pycache__/` bytecode files from git.
- Added `.gitignore` rules for Python caches and local virtual environments.

## Why

A damaged `config_complete.yaml` can either fail with a YAML parser error or, more dangerously, parse successfully while losing nested keys when comments and mappings are accidentally merged onto one line. That can make import/backtest scripts run with incomplete defaults or crash after partial work.

The import chain should fail before touching MongoDB or market data providers when config syntax or required structure is invalid.

Tracked `.pyc` files were also removed because they are environment-generated cache artifacts. Keeping them in git caused local verification runs to modify the working tree.

## How to verify

```bash
python3 scripts/data_import_pipeline.py config-check
python3 scripts/factor_data_import_service.py config-check
```

Expected output:

```text
CONFIG_OK: <path>/config/config_complete.yaml
```

If YAML is malformed or required keys are missing, the command exits with code `2` and prints `CONFIG_ERROR: ...`.

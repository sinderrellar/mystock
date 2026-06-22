# Strategy Effect Optimization TODO

## What changed

- Added a `Strategy Effect Optimization` section to `TODO.md`.
- Captured the follow-up work from the 2026-03/04/05 historical buy_plan forward tests:
  - data leakage audit
  - rolling sample validation
  - industry concentration control
  - entry overheating guard
  - regime-aware sizing
  - entry signal calibration

## Why

The historical samples showed that the current alpha rank can identify the 2026-04 to 2026-06 electronics/component theme, but the result also exposed structural risks:

- 2026-03-16 had weak short-term performance but recovered by T+20, suggesting entry timing can be early.
- 2026-04-15 and 2026-05-15 were very strong, which makes a data leakage audit mandatory before trusting the magnitude.
- Top recommendations were heavily concentrated in components/electronics, making executable portfolio construction riskier than raw alpha ranking.
- The entry engine was too permissive for top-ranked candidates and needs better overheating/crowding detection.

## Validation

- Documentation-only change.
- No runtime behavior changed.
- Based on historical forward test outputs for:
  - 2026-03-16
  - 2026-04-15
  - 2026-05-15

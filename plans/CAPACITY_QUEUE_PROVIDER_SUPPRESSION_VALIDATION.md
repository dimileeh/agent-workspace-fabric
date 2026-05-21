# Capacity Queue Provider Suppression Validation

Plan reference: `plans/CAPACITY_QUEUE_PROVIDER_SUPPRESSION_PLAN.md`

## Requirement Status

- Complete: Provider recovery `not_before` cooldown candidates are excluded
  from capacity blocker counts.
- Complete: Open provider/model circuit breaker candidates are excluded from
  capacity blocker counts.
- Complete: Queue totals and planned-resource behavior were not changed; the
  implementation filters only the blocker-count candidate simulation.
- Complete: Existing scheduler ordering and capacity accumulation behavior are
  preserved for eligible candidates.
- Complete: Regression tests were added before implementation and failed
  against the original behavior.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`
- `plans/CAPACITY_QUEUE_PROVIDER_SUPPRESSION_PLAN.md`
- `plans/CAPACITY_QUEUE_PROVIDER_SUPPRESSION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k 'provider_cooldown_candidates or open_provider_circuit_candidates'`
  - Failed before implementation with suppressed requests counted as
    `DIND_CAPACITY_SATURATED`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  - Passed: 90 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_metrics.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.

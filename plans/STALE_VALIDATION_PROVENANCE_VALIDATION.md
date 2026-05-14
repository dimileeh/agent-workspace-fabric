# Stale Validation Provenance Validation

Plan reference: `plans/STALE_VALIDATION_PROVENANCE_PLAN.md`

## Requirement Status

- Reproduce the stale callback overwrite with a focused unit regression:
  Complete. The new regression failed before the production fix with
  `STALE_CALLBACK_IGNORED != PYTEST_TEST_FAILURE`.
- Keep the original validation failure run and coverage as primary evidence:
  Complete. The regression asserts the original validation run id, reason code,
  and failing test coverage evidence remain attached.
- Exclude stale callback validation runs from automatic primary provenance
  attachment:
  Complete. `failure_causality` now filters ignored stale callback validation
  rows out of the primary validation provenance query.
- Do not weaken existing embedded-primary or secondary-failure behavior:
  Complete. The full failure-causality unit module passes.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/STALE_VALIDATION_PROVENANCE_PLAN.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_ignores_later_stale_validation_callback_run -q`
  failed before the fix as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_ignores_later_stale_validation_callback_run -q`
  passed after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed: 14 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Gaps

None.

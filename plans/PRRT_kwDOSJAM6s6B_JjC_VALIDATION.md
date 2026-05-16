# PRRT_kwDOSJAM6s6B_JjC Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6B_JjC_PLAN.md`

## Requirement Status

- Complete: Added a regression test where two failed validation runs have
  identical `finished_at` and `started_at`, while the lexicographically larger
  UUID-derived ID belongs to the older run.
- Complete: `_latest_failed_validation_run` now uses persisted chronological
  `created_at` and `updated_at` metadata after run timestamps tie, rather than
  `ValidationRun.id`.
- Complete: Existing stale callback filtering remains unchanged.
- Complete: Code changes are scoped to failure-causality validation run
  selection plus the regression test.

## Evidence

Files changed:

- `src/awf/service/failure_causality.py`
- `tests/unit/service/test_failure_causality.py`
- `plans/PRRT_kwDOSJAM6s6B_JjC_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6B_JjC_VALIDATION.md`

TDD evidence:

- First ran the new focused regression against the old UUID ordering:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_primary_failure_snapshot_does_not_tiebreak_validation_runs_by_random_id -q`
  and observed the expected failure selecting `OLD_PYTEST_FAILURE`.
- After implementation, the same focused regression passed.

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  passed: 33 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

## Gaps

None.

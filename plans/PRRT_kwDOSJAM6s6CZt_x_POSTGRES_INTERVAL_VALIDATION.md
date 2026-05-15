# PRRT_kwDOSJAM6s6CZt_x PostgreSQL Interval Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CZt_x_POSTGRES_INTERVAL_PLAN.md`

## Requirement Status

- Complete: PostgreSQL scheduler age-boost SQL now renders
  `make_interval(secs => CAST(... AS FLOAT))` instead of positional
  `make_interval` arguments.
- Complete: The seconds value remains built from `literal(float(seconds))`
  cast to `Float`, preserving a numeric value for PostgreSQL's `secs`
  argument.
- Complete: The raw interval text guard remains in place and the focused SQL
  regression still asserts that no `INTERVAL '...'` text is emitted.
- Complete: The fix is scoped to review thread `PRRT_kwDOSJAM6s6CZt_x` and
  ready for a local conventional commit.

## Evidence

Files changed:

- `src/awf/db/repositories.py`
- `tests/unit/db/test_workspace_repository.py`
- `plans/PRRT_kwDOSJAM6s6CZt_x_POSTGRES_INTERVAL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CZt_x_POSTGRES_INTERVAL_VALIDATION.md`

Verification commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_cursor_age_boost_uses_timestamp_thresholds -q`
  - Before implementation: failed because compiled SQL still used positional
    `make_interval` arguments.
  - After implementation: passed.
- PostgreSQL execution check using `$AWF_TEST_DATABASE_URL`:
  `select(repositories._postgresql_interval_seconds_expr(900))`
  - Passed and returned a 900-second interval.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py -q`
  - Passed: 71 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.

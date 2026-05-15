# PRRT_kwDOSJAM6s6CZt_x PostgreSQL Interval Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6CZt_x` flags
`_postgresql_interval_seconds_expr` for relying on positional
`make_interval` arguments and asks that the seconds argument remain numeric.

## Requirements Checklist

- Render PostgreSQL scheduler age-boost intervals with explicit
  `make_interval(secs => ...)` named notation.
- Preserve the numeric seconds cast so PostgreSQL receives a floating-point
  value for the `secs` argument.
- Keep the existing guard against raw interpolated interval text.
- Commit the thread-specific fix locally without pushing.

## Implementation Steps

1. Update the scheduler SQL regression to expect named `secs` notation.
2. Confirm the updated regression fails against the current helper.
3. Replace positional `make_interval` arguments with a fixed SQLAlchemy named
   argument expression.
4. Run the focused repository test file and lint for touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_cursor_age_boost_uses_timestamp_thresholds -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py`

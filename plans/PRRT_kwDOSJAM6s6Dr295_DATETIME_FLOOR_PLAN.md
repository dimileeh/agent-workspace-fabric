# PRRT_kwDOSJAM6s6Dr295 Datetime Floor Plan

## Problem Statement and Scope

The PR review reports that `_stale_active_execution_can_fail` passes the raw
timestamp returned by `_latest_preserved_active_execution_at` into
`_has_current_salvage_event`. Because that timestamp is selected as a bare
column value, dialect-specific timezone handling can differ from ORM-loaded
event attributes. The stale-active failure gate should normalize the preserved
timestamp before using it as the salvage event floor.

Scope is limited to the stale-active cleanup gate and its focused regression
coverage.

## Requirements Checklist

- Add a regression test proving `_stale_active_execution_can_fail` normalizes a
  naive latest-preserved timestamp before checking blocking salvage events.
- Normalize the latest preserved timestamp with the existing `_utc_datetime`
  helper before using it as the salvage event floor.
- Keep behavior for stale detection, preservation expiry, and status-scoped
  salvage checks unchanged.
- Run the narrow regression test and the focused worker coverage test file.

## Implementation Steps

1. Add a failing test in `tests/unit/control/test_worker_coverage_edges.py`.
2. Update `_stale_active_execution_can_fail` to reuse a normalized
   `latest_preserved` value for salvage checks and expiry.
3. Run the targeted regression test, then the worker coverage edge test file.
4. Record validation evidence in the matching validation artifact.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_stale_active_execution_can_fail_normalizes_latest_preserved_floor -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q
```

Pass criteria: both commands pass.

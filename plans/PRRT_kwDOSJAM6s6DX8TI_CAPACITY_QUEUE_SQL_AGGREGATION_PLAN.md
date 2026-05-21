# PRRT_kwDOSJAM6s6DX8TI Capacity Queue SQL Aggregation Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DX8TI` reports that capacity queue blocked
reason metrics fetch all requested `Workspace` ORM rows and aggregate in Python.
Scope is the requested-workspace blocked-reason count path in
`src/awf/service/metrics.py`.

## Requirements Checklist

- Add regression coverage proving queue blocked-reason counts are computed with
  one aggregate SQL query rather than loading requested workspace ORM rows.
- Preserve existing blocked-reason behavior, including fallback defaults for
  requested workspaces without an active reservation.
- Preserve latest-active-reservation semantics for requested workspaces with
  multiple active reservation rows.
- Preserve configured and detected local capacity limit behavior.
- Commit the scoped fix locally with a conventional commit message for the
  review thread.

## Implementation Steps

1. Add a focused unit regression around `_capacity_queue_blocked_reason_counts`.
2. Confirm the regression fails against the current implementation.
3. Replace the Python per-workspace loop with a single SQL aggregate query using
   latest-active reservation subquery rows, `coalesce`, and `sum(case(...))`.
4. Run the narrow regression and relevant metrics tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_aggregates_requested_demands_in_sql -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  passes.

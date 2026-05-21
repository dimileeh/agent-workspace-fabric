# PRRT_kwDOSJAM6s6DX8TI Capacity Queue SQL Aggregation Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DX8TI_CAPACITY_QUEUE_SQL_AGGREGATION_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving `_capacity_queue_blocked_reason_counts`
  uses one aggregate SQL query and does not load requested workspace ORM rows.
- Complete: Preserved fallback default demand for requested workspaces without
  active reservations.
- Complete: Preserved latest-active-reservation semantics for requested
  workspaces with multiple active reservation rows.
- Complete: Preserved configured and detected capacity limit behavior in the
  SQL aggregation path.
- Complete: Prepared the scoped code/test/plan changes for a local conventional
  commit.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`
- `plans/PRRT_kwDOSJAM6s6DX8TI_CAPACITY_QUEUE_SQL_AGGREGATION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DX8TI_CAPACITY_QUEUE_SQL_AGGREGATION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_aggregates_requested_demands_in_sql -q`
  - Failed before implementation: current code issued multiple statements while
    loading requested workspace ORM rows.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  - Passed: 81 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  - Passed: 11 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/metrics.py`
  - Passed.

## Gaps

None.

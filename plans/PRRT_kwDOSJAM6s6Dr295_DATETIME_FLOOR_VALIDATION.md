# PRRT_kwDOSJAM6s6Dr295 Datetime Floor Validation

Plan reference: `PRRT_kwDOSJAM6s6Dr295_DATETIME_FLOOR_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving `_stale_active_execution_can_fail`
  normalizes a naive latest-preserved timestamp before checking blocking
  salvage events.
- Complete: Normalized the latest preserved timestamp with `_utc_datetime`
  before using it as the salvage event floor and expiry input.
- Complete: Kept stale detection, preservation expiry, and status-scoped salvage
  behavior unchanged by limiting the code change to the existing
  `latest_preserved is not None` branch.
- Complete: Ran the targeted regression test and the focused worker coverage
  edge test file.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker_coverage_edges.py`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_stale_active_execution_can_fail_normalizes_latest_preserved_floor -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker_coverage_edges.py
```

Result: all commands passed after the implementation change. The targeted
regression failed before the worker change, confirming the test covers the
reviewed issue.

## Remaining Gaps

None.

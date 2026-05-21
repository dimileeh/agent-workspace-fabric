# PRRT_kwDOSJAM6s6Dkfij Capacity Priority Scan Validation

Plan reference: `PRRT_kwDOSJAM6s6Dkfij_CAPACITY_PRIORITY_SCAN_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `blocked_reason_counts` applies
  scheduler priority before the bounded candidate limit.
- Complete: Kept the scan bounded in SQL and preserved the latest active
  reservation join behavior.
- Complete: Reused scheduler SQL ordering semantics via the existing scheduler
  order expression helper.
- Complete: Preserved provider recovery filtering and final in-memory ordering
  semantics.
- Complete: Scope is limited to the review-thread files plus required plan and
  validation documents.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/service/test_metrics.py`
- `plans/PRRT_kwDOSJAM6s6Dkfij_CAPACITY_PRIORITY_SCAN_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Dkfij_CAPACITY_PRIORITY_SCAN_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_limits_after_scheduler_priority -q`
  - Failed before implementation with `{}` instead of
    `{"PEAK_CPU_CAPACITY_SATURATED": 1}`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_limits_after_scheduler_priority -q`
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  - Passed: 8 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `git diff --check`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  - Passed: 92 tests.

## Gaps

None.

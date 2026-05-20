# Review 4495131102 Allocated Guard Validation

Plan reference: `plans/REVIEW_4495131102_ALLOCATED_GUARD_PLAN.md`

## Requirement Status

- Complete: Skip scheduler-allocation resource queries when no local capacity
  constraints are explicitly configured.
  - Evidence: `_capacity_queue_summary` now checks the shared local capacity
    constraint contract before calling `_scheduler_allocated_resources_for_session`.
- Complete: Preserve existing capacity queue totals.
  - Evidence: the new regression asserts queued count, oldest workspace, wait
    age, and planned requested resources still populate without configured
    constraints.
- Complete: Preserve blocker-count behavior when capacity constraints are
  configured.
  - Evidence: focused existing `capacity_queue_blocked_reason_counts` and
    resource saturation capacity tests pass.
- Complete: Add regression coverage for the reviewed hot-path issue.
  - Evidence: `test_capacity_queue_summary_skips_scheduler_allocation_when_unconstrained`
    failed before implementation because the monkeypatched scheduler-allocation
    function was called, then passed after the guard.
- Complete: Commit-ready local scoped fix.
  - Evidence: changed files are limited to the metrics service, focused service
    tests, and plan/validation records.

## Verification Evidence

- Failed before implementation as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_summary_skips_scheduler_allocation_when_unconstrained -q`
  - Failure: `AssertionError: scheduler allocation should not be loaded without capacity limits`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_summary_skips_scheduler_allocation_when_unconstrained -q`
  - `1 passed`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_summary or capacity_queue_blocked_reason_counts or resource_saturation_scopes_capacity_view_to_local_node or capacity_queue_uses_scheduler_allocation_scope"`
  - `11 passed, 84 deselected`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/service/metrics.py`

## Remaining Gaps

None.

# Stale Validation Cleanup Guard Validation

Plan reference: `plans/STALE_VALIDATION_CLEANUP_GUARD_PLAN.md`

## Requirement Status

- Complete: Added a regression test where validation cleanup fails while the
  validation callback is stale, covering both already-stale and becomes-stale
  timings.
- Complete: Preserved the existing cleanup-success stale-callback behavior.
- Complete: Cleanup failures now flow through `_fail_validation_worktree_guard`
  even when stale callback handling has already occurred.
- Complete: Validation evidence stayed focused to the changed executor test
  area; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Evidence

- Changed `src/awf/control/executor/execution_validation.py`.
- Changed `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`.
- Added this plan/validation pair for protocol traceability.

## Commands

- Failing-first evidence before production fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  failed because `_finish_validation_run` was never awaited.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  passed with `2 passed`.
- Neighboring behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_stops_if_callback_becomes_stale_after_cleanup_exception tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  passed with `3 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`
  passed.

## Gaps

None.

# Review PRRT_kwDOSJAM6s6GGhde Stale Cleanup Double Finish Plan

## Problem Statement And Scope

An unresolved PR review thread reports that validation cleanup failure handling
can finish the same validation run twice when a stale validation callback has
already closed the run with `STALE_CALLBACK_IGNORED`. Scope is limited to the
executor validation cleanup guard and targeted regression coverage.

## Requirements Checklist

- Reproduce the bug with a focused regression test before changing production
  code.
- Preserve stale cleanup secondary-failure evidence recording.
- Avoid reclosing a validation run that `_finish_validation_callback_if_terminal`
  has already terminally failed as stale.
- Keep validation local and focused; broad AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Update the existing stale cleanup failure regression to assert that the
   cleanup guard does not call `_finish_validation_run` again after a stale
   callback is observed.
2. Run the targeted test and confirm it fails on the current implementation.
3. Pass `validation_run_id=None` into `_fail_validation_worktree_guard` when
   `stale_cleanup_callback_ignored` is true, while still recording secondary
   cleanup evidence with the original validation run id.
4. Re-run the targeted test and any adjacent focused stale-cleanup tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  - Passes after implementation and fails before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_stale_cleanup.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  - Passes after implementation.

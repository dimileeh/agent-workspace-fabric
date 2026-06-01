# Stale Validation Cleanup Guard Plan

## Problem Statement

PR review thread `PRRT_kwDOSJAM6s6GF1h6` reports that validation cleanup failures
are only logged when a validation callback becomes stale. The cleanup failure
should still be persisted through the validation worktree guard so later repair
or monitor flows see a durable reason instead of an unexplained dirty worktree.

## Scope

- Touch only the validation cleanup guard behavior and its focused regression
  coverage.
- Do not change branch, push, rebase, or run broad AWF/GitHub-owned validation.

## Requirements Checklist

- Add a regression test where cleanup fails after
  `_finish_validation_callback_if_terminal` reports a stale callback.
- Preserve existing stale-callback behavior when cleanup succeeds.
- Persist cleanup failures through `_fail_validation_worktree_guard`, including
  the cleanup reason code, even when the callback is stale.
- Keep validation evidence focused to the changed test area.

## Implementation Steps

1. Add a failing regression test in the existing executor validation coverage
   tests for stale callback plus failed cleanup.
2. Run that single test and confirm it fails before changing production code.
3. Update `_handle_validation_cleanup_guard` so cleanup failures invoke the
   worktree guard regardless of callback staleness.
4. Run the focused regression and the neighboring stale-callback cleanup test.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_stops_if_callback_becomes_stale_after_cleanup_exception tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`

Full AWF/GitHub validation remains owned by AWF after agent completion.

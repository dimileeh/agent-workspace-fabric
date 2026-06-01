# Validation Cleanup Non-Plan Output Plan

## Problem Statement and Scope

The PR review reports that validation-worktree guard failures can return
`has_known_non_plan_output=False` even when `run_validation_and_fix_cycle`
received `has_known_non_plan_output=True`. Scope is limited to preserving that
flag through fatal validation-worktree guard exits and the cleanup guard
delegation.

## Requirements Checklist

- Add regression coverage showing a dirty validation-worktree guard preserves
  `has_known_non_plan_output=True`.
- Add regression coverage showing cleanup guard failure preserves
  `has_known_non_plan_output=True`.
- Preserve existing validation run finalization, pending operation failure, and
  workspace failure behavior.
- Do not run broad AWF/GitHub-owned validation; use focused unit tests only.

## Implementation Steps

1. Update focused unit tests to assert the flag is preserved in the affected
   direct guard and cleanup guard paths.
2. Confirm the new focused tests fail before implementation when practical.
3. Thread `has_known_non_plan_output` through `fail_validation_worktree_guard`
   and all local callers in `execution_validation.py` and
   `validation_cleanup_guards.py`.
4. Run the focused unit tests that cover the changed paths.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py::test_execution_validation_fails_when_worktree_is_dirty_before_starting_run tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py::test_execution_validation_fails_cleanup_when_callback_becomes_stale_after_cleanup_exception -q`
  - Passes after implementation.
  - Fails before implementation due to `has_known_non_plan_output` being
    incorrectly cleared.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.

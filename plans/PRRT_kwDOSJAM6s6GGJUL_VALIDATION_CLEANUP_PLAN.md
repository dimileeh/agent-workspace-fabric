# PRRT_kwDOSJAM6s6GGJUL Validation Cleanup Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GGJUL` reports that validation worktree cleanup failures can be lost when the validation callback has already been treated as stale. The current cleanup guard logs the stale cleanup failure, then calls the normal validation-worktree failure path, which marks failure only from `WorkspaceStatus.validating`. Once the workspace is already terminal, that guarded transition is skipped and the cleanup failure may not be durably represented in workspace failure causality.

Scope is limited to validation cleanup failure handling in `src/awf/control/executor/execution_validation.py` and focused regression coverage for stale-callback cleanup failure persistence.

## Requirements Checklist

- Preserve the existing normal validation cleanup failure behavior while the workspace is still `validating`.
- When cleanup fails after a stale validation callback, record durable workspace timeline evidence for the cleanup failure even though the workspace must not transition from its current terminal status.
- Preserve primary failure row fields and primary failure causality when a prior failure already exists.
- Include the validation run id and cleanup details in the secondary cleanup-failure evidence where available.
- Add a regression test that fails before the implementation and proves stale cleanup failure evidence is durable.
- Run only focused validation for the changed behavior; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a focused failing regression around validation cleanup failure after a stale callback.
2. Add a helper in `execution_validation.py` to record stale cleanup failures as secondary failure events when the normal validating-to-failed transition is no longer valid.
3. Route `_handle_validation_cleanup_guard` through the new helper after `_finish_validation_callback_if_terminal` reports the callback as stale.
4. Keep current `_fail_validation_worktree_guard` behavior for non-stale cleanup failures.
5. Update validation documentation with requirement status and focused command evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py -q`
  - Passes with the stale cleanup regression and adjacent edge tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py -q`
  - Passes if a DB-backed regression is added there; otherwise not required.
- Full AWF/GitHub validation is intentionally not run in-agent per workspace contract.

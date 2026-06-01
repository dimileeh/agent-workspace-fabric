# Review 4587922231 Validation Worktree Guard Plan

## Problem Statement And Scope

PR review comment `issue:4587922231` reports two validation worktree guard defects:

- The executor cleanup guard could double-finish a validation run after a stale callback and cleanup failure.
- The PR monitor pre-push fix loop can commit a successful fix while leaving new ignored artifacts, then reject the next validation pass as `VALIDATION_WORKTREE_PRE_EXISTING_DIRTY` because those artifacts were absent from the initial ignored snapshot.

Scope is limited to the local guard behavior and focused regressions for these two paths. Do not run AWF/GitHub-owned broad validation; AWF handles broad validation after agent completion.

## Requirements Checklist

- Confirm the stale-callback cleanup path does not finish the already-closed validation run a second time.
- Preserve existing regression coverage for the stale-callback cleanup path.
- Add regression coverage showing a successful pre-push fix-pass commit cleans ignored artifacts before the next validation retry.
- Preserve terminal failure behavior when cleanup after a successful fix-pass commit fails.
- Keep changes scoped to `pre_push_validation.py`, targeted tests, and plan/validation docs.
- Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Inspect current executor cleanup guard and existing stale-callback tests.
2. Add a focused pre-push fix-pass test that simulates a committed fix pass followed by successful cleanup of new ignored artifacts.
3. Update the pre-push fix-pass flow to clean validation/fix-pass side effects after a successful commit using the new committed HEAD as the restore ref and the initial ignored baseline/snapshot.
4. Ensure cleanup failure after a committed fix pass returns a terminal reason to the caller instead of continuing into another validation retry.
5. Run only targeted unit tests covering the changed PR monitor path and existing stale-callback guard regression.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py -q`
  - Passes with the new cleanup regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py -q`
  - Passes existing stale-callback cleanup guard regressions.

Full AWF/GitHub validation is intentionally not run in this agent phase per workspace contract.

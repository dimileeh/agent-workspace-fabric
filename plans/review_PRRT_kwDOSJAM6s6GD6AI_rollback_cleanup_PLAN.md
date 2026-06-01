# PRRT_kwDOSJAM6s6GD6AI Rollback Cleanup Plan

## Problem Statement And Scope

An unresolved review thread reports that a pre-push validation fix pass labels a
successful rollback reset followed by failed validation-worktree cleanup as
`PRE_PUSH_VALIDATION_ROLLBACK_FAILED`. That conflates an unrecovered reset
failure with cleanup failure after the reset already restored tracked state.

Scope is limited to pre-push validation fix-pass rollback reporting and focused
unit coverage. No broad AWF or CI-equivalent validation will be run in the agent
phase.

## Requirements Checklist

- Add a regression test for a failed fix-pass commit where `git reset --hard`
  succeeds but validation-worktree cleanup fails.
- Preserve `PRE_PUSH_VALIDATION_ROLLBACK_FAILED` only for failed rollback reset.
- Surface post-reset validation-worktree cleanup failure with the cleanup
  reason code, such as `VALIDATION_WORKTREE_CLEANUP_FAILED`.
- Keep existing fix-pass failure behavior and terminal rollback behavior intact.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Add a focused unit test in `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
   that scripts successful rollback reset and failed cleanup after a fix commit
   failure.
2. Update the rollback helper to return a failure reason code instead of a
   boolean so reset failure and cleanup failure remain distinguishable.
3. Update fix-pass callers to propagate the specific rollback or cleanup reason.
4. Adjust existing tests that directly assert the rollback helper result.

## Verification Commands And Pass Criteria

- Run the new regression test first and confirm it fails before the code change.
- Run the focused pre-push validation tests affected by the helper change:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q -k "fix_pass_rollback or cleanup_failure_blocks_push or commit_fail_returns_fix_failed_reason_code"`
- Pass criteria: focused tests pass, the new regression fails before the fix and
  passes after it, and no broad AWF/GitHub validation is executed locally.

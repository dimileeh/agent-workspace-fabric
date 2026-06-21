# Pre-Push Recovery Anchor Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6K1FS3` reports that pre-push missing-HEAD
recovery prefers `operation_start_head` without first validating that the commit
object exists. If that captured anchor is dangling, recovery can fail even when
the open merge candidate head is still valid.

Scope is limited to the pre-push missing-HEAD recovery anchor choice and focused
regression coverage.

## Requirements Checklist

- Validate a preferred `operation_start_head` recovery anchor before using it.
- Fall back to the open merge candidate head when the preferred anchor is absent
  or dangling.
- Preserve existing unrecoverable behavior when no valid recovery anchor exists.
- Add focused regression coverage for the fallback.
- Run only targeted validation for the changed behavior; AWF/GitHub own broad
  validation after this agent phase.

## Implementation Steps

1. Add a failing regression in the pre-push validation edge tests.
2. Import and use the existing mirror commit-object validation helper.
3. Mirror `_commit_dirty_worktree` fallback semantics in `_run_pre_push_validation`.
4. Run the targeted regression test.
5. Record validation evidence in `PRE_PUSH_RECOVERY_ANCHOR_VALIDATION.md`.

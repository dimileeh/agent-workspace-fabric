# PRRT_kwDOSJAM6s6GEIV7 Status Failure HEAD Rollback Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GEIV7` reports that
`cleanup_validation_worktree_side_effects` returns immediately for
`VALIDATION_WORKTREE_STATUS_FAILED` on both the initial cleanliness check and
the post-cleanup verify pass. Those returns bypass `_verify_head_unchanged`,
so validation-authored HEAD movement can be left stranded instead of being
rolled back to `restore_ref`.

Scope is limited to validation worktree cleanup behavior and focused
regressions in `tests/unit/runtime/test_validation_worktree.py`.

## Requirements Checklist

- Add regression coverage proving an initial status failure still verifies and
  rolls back HEAD when `restore_ref` is available.
- Add regression coverage proving a post-cleanup verify status failure still
  verifies and rolls back HEAD when `restore_ref` is available.
- Preserve existing status-failure reason-code behavior when HEAD is unchanged.
- Reuse the existing `_verify_head_unchanged` rollback path instead of adding a
  second rollback implementation.
- Run only focused validation for the changed behavior; full AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add the two focused failing tests for initial and verify status-failure
   rollback paths.
2. Move the `_return_after_head_verification` helper above the initial status
   failure branch and route that branch through it.
3. Route the verify status-failure branch through the same helper.
4. Run the targeted test module or focused test selectors that cover the new
   regressions and nearby existing behavior.
5. Record evidence in the matching validation document.

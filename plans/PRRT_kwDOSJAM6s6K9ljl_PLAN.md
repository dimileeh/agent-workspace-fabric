# PRRT_kwDOSJAM6s6K9ljl Plan

## Problem Statement

The PR monitor comment-agent wrapper repairs a poisoned mirror hooks path after a
non-`AgentRunError` adapter/runtime failure, then rethrows before the existing
dirty-worktree sink can verify and recover a missing HEAD object. If the agent
self-committed with private Git object storage before the plumbing failure, the
workspace ref can remain pointed at an object missing from the canonical mirror.

## Scope

- Update only the comment-agent verdict wrapper path for non-`AgentRunError`
  failures.
- Reuse the existing dirty-worktree sink so its current missing-HEAD
  verification and recovery behavior runs before the original failure is
  rethrown.
- Preserve the original exception after recovery/verification unless the mirror
  hooks repair itself fails.
- Add/update focused regression coverage for this cleanup failure path.

## Requirements Checklist

- [ ] A non-`AgentRunError` after the agent starts still repairs the mirror hooks
      path when a mirror exists.
- [ ] The wrapper invokes `_commit_dirty_worktree` after that repair, passing the
      same operation start HEAD and command context used by the normal path.
- [ ] The original runtime/plumbing exception is still rethrown after the
      verification/recovery sink runs.
- [ ] Focused tests demonstrate the new call order without running broad AWF/CI
      validation.

## Implementation Steps

1. Update the existing cleanup-failure test to expect the dirty-worktree sink
   after the post-agent mirror hooks repair and before the exception propagates.
2. Run that focused test to confirm it fails against the current code.
3. Change `src/awf/runtime/pr_monitor_runner/comments.py` to call
   `_commit_dirty_worktree` in the generic exception path after mirror repair.
4. Re-run the focused test.
5. Record focused validation evidence in
   `plans/PRRT_kwDOSJAM6s6K9ljl_VALIDATION.md`.

## Assumptions/Changes

- The original non-`AgentRunError` is rethrown when `_commit_dirty_worktree`
  completes. If the sink itself raises a safety exception such as unrecoverable
  missing HEAD, that existing sink failure is allowed to supersede the original
  runtime/plumbing exception, matching the normal sink path's fail-closed
  behavior.

# PRRT_kwDOSJAM6s6K7tS6 Plan

## Goal

Preserve the `MIRROR_HOOKS_PATH_POISONED` reason when `_commit_dirty_worktree`
raises `_MonitorMirrorHooksPathRepairFailedError` during pre-push dirty finalize.

## Steps

1. Add a focused regression covering dirty-finalize commit-sink mirror hook-path
   repair failure.
2. Confirm the regression fails before the implementation change.
3. Add the smallest explicit dirty-finalize exception handler that returns a
   non-clean `ValidationWorktreeCheck` carrying the mirror hook-path reason.
4. Run the targeted regression test. Full AWF/GitHub validation remains owned by
   AWF after agent completion.

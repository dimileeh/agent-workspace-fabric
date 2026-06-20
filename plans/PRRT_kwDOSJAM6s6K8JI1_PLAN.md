# PRRT_kwDOSJAM6s6K8JI1 Plan

## Review Claim

Dirty finalize preserves `_MonitorMirrorHooksPathRepairFailedError` from
`_commit_dirty_worktree`, but does not preserve `_MonitorHeadObjectMissingError`.
If HEAD loses its commit object before `_commit_dirty_worktree`, the broad
`except Exception` returns `None`, causing the caller to report the stale dirty
worktree reason instead of `HEAD_OBJECT_MISSING_UNRECOVERABLE`.

## Scope

- Add a focused dirty-finalize regression proving `_MonitorHeadObjectMissingError`
  becomes the pre-push result reason code.
- Add the minimal catch block in
  `src/awf/runtime/pr_monitor_runner/pre_push_validation_dirty_finalize.py`.
- Keep existing dirty-finalize behavior otherwise unchanged.

## Validation

- Run the new focused pytest selection before the fix to confirm the failure.
- Run the same focused pytest selection after the fix.
- Run focused ruff on the touched source and test files.
- Full AWF/GitHub validation is managed by AWF after agent completion.

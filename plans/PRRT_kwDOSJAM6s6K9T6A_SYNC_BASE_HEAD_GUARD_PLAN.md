# PRRT_kwDOSJAM6s6K9T6A Sync-Base HEAD Guard Plan

## Problem Statement and Scope

The sync-base conflict-agent path repairs mirror hooks after a non-`AgentRunError`
adapter/runtime failure, then immediately re-raises the original exception. That
skips `_commit_dirty_worktree`, whose HEAD-object verification can detect and
recover from an agent self-commit made through private Git object lookup
environment. The fix is scoped to the sync-base conflict-agent cleanup/error path
in `remote_ops.py` and its focused unit coverage.

## Requirements Checklist

- Preserve the existing fail-closed behavior when post-agent mirror hook repair
  fails.
- After successful post-agent mirror hook repair for a non-`AgentRunError`,
  invoke the dirty-worktree commit sink so its HEAD-object guard can verify or
  recover HEAD before the original exception propagates.
- Propagate `_MonitorHeadObjectMissingError`,
  `_MonitorMirrorHooksPathRepairFailedError`, and other sink failures according
  to the existing sync-base sink handling.
- Preserve the original adapter/runtime exception when the sink succeeds.
- Keep validation focused; do not run AWF/GitHub-owned broad validation.

## Implementation Steps

1. Add or update a sync-base regression test for a post-agent cleanup failure
   that succeeds hook repair and must call `_commit_dirty_worktree` with
   `operation_start_head`.
2. Confirm the targeted regression fails before the implementation.
3. Update `_run_sync_base` so the generic post-agent exception path records the
   original exception, runs the existing dirty-worktree sink and sink exception
   mapping, then re-raises the original exception after the guard succeeds.
4. Run the focused sync-base test file or the affected tests only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  - Passes after implementation.
  - Full AWF/GitHub validation remains managed by AWF after agent completion.

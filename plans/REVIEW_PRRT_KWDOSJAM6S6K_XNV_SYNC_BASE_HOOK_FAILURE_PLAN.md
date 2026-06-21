# Review PRRT_kwDOSJAM6s6K-XnV Sync-Base Hook Failure Plan

## Problem Statement and Scope

The PR review reports that `_run_sync_base` can raise `_MonitorMirrorHooksPathRepairFailedError` from the post-agent mirror hook repair path before entering `_commit_dirty_worktree`, bypassing the later handler that converts this failure into `_GitPushResult`.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_ops.py` and the focused sync-base regression test covering post-agent hook repair failure.

## Requirements Checklist

- Confirm the reported escape path exists in actual code.
- Return a structured failed `_GitPushResult` with `MIRROR_HOOKS_PATH_POISONED` when post-agent sync-base mirror hook repair fails.
- Preserve existing fail-closed behavior: do not run dirty-worktree commit, protected-scope checks, or push after the post-agent repair failure.
- Cover the behavior with a focused regression test.
- Run only targeted validation for the changed behavior; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Update the post-agent `repair_mirror_hooks_path` failure branch in `_run_sync_base`.
2. Adjust the existing post-agent sync-base mirror hook failure test to assert the returned `_GitPushResult`.
3. Run the targeted test for the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py::test_run_sync_base_fails_closed_when_post_agent_mirror_hooks_repair_fails -q`
- Pass criteria: the focused regression passes and confirms the failure is structured with `MIRROR_HOOKS_PATH_POISONED`.

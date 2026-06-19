# Sync-Base Mirror Hooks Repair Plan

## Problem Statement and Scope

PR review comment `4532603325` reports that sync-base clean merges run
`git merge --no-edit` before repairing a poisoned shared mirror
`core.hooksPath`. Because `git merge` can invoke merge/commit hooks while
authoring the AWF merge commit, the mirror hooks path must be repaired before
the sync-base merge command.

Scope is limited to the PR monitor sync-base path and focused regression
coverage for hook-repair ordering and failure handling.

## Requirements Checklist

- Repair the worktree mirror hooks path before the sync-base `git merge
  --no-edit` command.
- If mirror hook repair fails, return a failed `_GitPushResult` with the
  existing mirror-hooks reason code and do not run the merge.
- Preserve existing sync-base conflict, protected-scope, and validated-push
  behavior.
- Add focused regression tests for the ordering and failure behavior.

## Implementation Steps

1. Import the existing mirror path and hook repair helpers into
   `remote_ops.py`.
2. In `_run_sync_base`, after base fetch and before assembling/running merge
   args, repair the mirror hooks path when the worktree has a shared mirror.
3. Convert `GitOperationError`/`OSError` from that repair into the existing
   `_MIRROR_HOOKS_PATH_POISONED_REASON` failed push result.
4. Add unit tests that prove repair precedes clean merge and that repair
   failure blocks the merge.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_sync_base_operation_start_head.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per workspace contract.

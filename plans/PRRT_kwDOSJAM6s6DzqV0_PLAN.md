# PRRT_kwDOSJAM6s6DzqV0 Ownership-repair failure must fail hard in sync and repair paths

## Problem Statement and Scope

A failed `_repair_agent_runtime_ownership` call in `_commit_dirty_worktree` returns `False` in
some branches and can be interpreted as a non-fatal no-op by upstream callers.
Scope is limited to PR-monitor commit helpers for review thread fixes, sync-base, CI-fix,
and protected-scope repair pre-push paths.

## Requirements Checklist

- Treat ownership-repair failure in `_commit_dirty_worktree` as an explicit failure outcome,
  not a no-op.
- Propagate that failure to callers that commit dirty worktree changes before pushing:
  `_run_fix_cycle` (thread/comment address paths), `_run_sync_base`, and
  `_repair_protected_scope_commits_before_push`.
- Ensure push results carry `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE` instead of
  being silently considered successful/no-op.
- Add unit tests proving each call path emits hard-failure push results.

## Implementation Steps

1. Add a focused monitor-specific exception for ownership-repair failure with a
   `reason_code` property.
2. In `_commit_dirty_worktree`, raise this exception on failed ownership repair (pre-commit,
   post-commit cleanup, post-commit validation).
3. Add exception handling in all callers that can now receive this failure and return
   `_GitPushResult(failed=True, pushed=False, reason_code=...)` with returncode 1.
4. Add/adjust coverage tests for:
   - direct `_commit_dirty_worktree` fail behavior,
   - `_run_fix_cycle` thread path,
   - `_run_sync_base` conflict path,
   - protected-scope committed-edit repair path.

## Verification Commands and Pass Criteria

- `pytest` execution for touched tests (targeted) passes after fix.
- `ruff`/`mypy` pass on changed files.

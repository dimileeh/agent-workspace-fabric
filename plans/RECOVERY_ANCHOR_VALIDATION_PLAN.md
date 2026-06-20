# Recovery Anchor Validation Plan

## Problem Statement And Scope

An unresolved PR review thread reports that missing-HEAD filesystem recovery in
`remote_repair.py` always prefers `operation_start_head` even when that SHA was
captured from a poisoned worktree and no longer resolves to a commit object in
the mirror. In that case recovery fails before trying the open merge candidate
head.

Scope is limited to missing-HEAD dirty-worktree recovery in
`src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused unit coverage
for that behavior.

## Requirements Checklist

- Validate a non-empty `operation_start_head` against the mirror before using it
  as the filesystem recovery anchor.
- Fall back to `_open_merge_candidate_head_sha` when the captured
  `operation_start_head` is not resolvable as a commit.
- Preserve existing behavior when `operation_start_head` is resolvable or when no
  fallback head exists.
- Add focused regression coverage for the stale-anchor fallback path.
- Run only targeted validation; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a small mirror `cat-file -e <sha>^{commit}` validation helper in
   `remote_repair.py`.
2. Use the helper in `_commit_dirty_worktree` before choosing the recovery head.
3. Add a unit regression in `test_pr_monitor_runner_part_005.py` proving stale
   operation-start SHAs fall back to the merge candidate head.
4. Run the focused unit test file or specific test(s) touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q`

Pass criteria: the focused unit test file passes, including the new regression.

# Sync Base Remote Diff Base Plan

## Problem Statement And Scope

CodeRabbit review comment `4322884631` reports that sync-base protected-scope validation filters candidate paths from the remote PR branch delta, but builds `ProtectedFileDiff` payloads from the merged base-branch baseline. That can reclassify already-remote PR edits as if they were authored by the local sync-base pass.

Scope is limited to `src/awf/runtime/pr_monitor_runner.py` and a focused unit regression in the existing PR monitor runner coverage tests.

## Requirements Checklist

- Verify the finding against current code.
- Add a failing regression showing sync-base protected file diffs use the remote PR branch baseline for `git show` and `git diff`.
- Reuse the remote branch diff baseline already computed for `changed_from_remote`.
- Preserve the merged base-branch diff only as the filter for changes still present relative to base.
- Run focused validation for the regression.

## Implementation Steps

1. Add a unit test around `_protected_scope_violations_for_sync_base_push` that queues a protected workflow path in both remote-branch and base-branch diffs.
2. Assert the subsequent protected diff commands use the remote branch merge-base ref.
3. Update `_protected_scope_violations_for_sync_base_push` to call `_remote_branch_diff_base_and_changed_paths` directly and carry its returned baseline into `_protected_file_diffs_for_committed_paths`.
4. Run the focused unit test, then a narrow related test slice if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "sync_base_protected_scope"`

Pass criteria: the new regression and nearby sync-base protected-scope tests pass.

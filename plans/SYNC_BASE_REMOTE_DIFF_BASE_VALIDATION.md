# Sync Base Remote Diff Base Validation

Plan reference: `plans/SYNC_BASE_REMOTE_DIFF_BASE_PLAN.md`

## Requirement Status

- Verify the finding against current code: Complete. `_protected_scope_violations_for_sync_base_push` used `merged_base` for protected diff payloads while deriving authored paths from the remote branch delta.
- Add a failing regression: Complete. `test_sync_base_protected_scope_diffs_use_remote_branch_base` failed before the implementation change because `git show`/`git diff` used the merged-base ref.
- Reuse the remote branch diff baseline: Complete. The sync-base path now carries `remote_branch_base` from `_remote_branch_diff_base_and_changed_paths` into `_protected_file_diffs_for_committed_paths`.
- Preserve merged base-branch filtering: Complete. The refreshed base branch merge-base still filters `sync_base_authored_paths` to paths also changed relative to base.
- Run focused validation: Complete.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner.py`.
- Added regression coverage in `tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`.
- Initial red test:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "sync_base_protected_scope_diffs_use_remote_branch_base"`
  - Failed because `remote-branch-base-sha:.github/workflows/ci.yml` was absent from recorded git calls.
- Passing validation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py -q -k "sync_base_protected_scope"`: 3 passed.
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py`: passed.

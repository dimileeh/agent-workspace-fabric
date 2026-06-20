# Review PRRT_kwDOSJAM6s6K_x87 Malformed Recovered Diff Cleanup Validation

Plan reference: `REVIEW_PRRT_KWDOSJAM6S6K_X87_MALFORMED_RECOVERED_DIFF_CLEANUP_PLAN.md`

## Requirement Status

- Verify the reported branch exists and is not already cleaned up: Complete.
  The `ProtectedScopeDiffError` handler in `src/awf/runtime/pr_monitor_runner/remote_repair.py` logged and raised without calling `_cleanup_recovered_missing_head_delta`.
- Extend the malformed recovered-diff regression to require a reset to `operation_start_head`: Complete.
  Updated `test_commit_dirty_worktree_rejects_malformed_recovered_diff` to expect `git reset --hard` after malformed recovered diff output.
- Call the existing recovered missing-HEAD cleanup helper before raising for malformed recovered diff output: Complete.
  The malformed-output exception branch now calls `_cleanup_recovered_missing_head_delta` with reason `recovered_diff_malformed`.
- Keep changes minimal and avoid broad AWF/GitHub-owned validation: Complete.
  Only the relevant runner branch, existing focused test, and required plan/validation docs changed.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K_X87_MALFORMED_RECOVERED_DIFF_CLEANUP_PLAN.md`
- `plans/REVIEW_PRRT_KWDOSJAM6S6K_X87_MALFORMED_RECOVERED_DIFF_CLEANUP_VALIDATION.md`

Focused validation:

- Before implementation, the updated regression failed because only the recovered diff command ran and no reset occurred.
- After implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_commit_dirty_worktree_rejects_malformed_recovered_diff -q` passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation and merge-gating after completion.

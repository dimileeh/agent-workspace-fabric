# Review PRRT_kwDOSJAM6s6K_x87 Malformed Recovered Diff Cleanup Plan

## Problem Statement And Scope

The PR review reports that `_commit_dirty_worktree` cleans up a recovered missing-HEAD delta when `git diff` fails, but not when `git diff --name-status -z` exits successfully with malformed output. In that malformed-output path, the monitor raises `_MonitorHeadObjectMissingError` while leaving the worktree on the unvalidated recovered HEAD.

Scope is limited to the malformed recovered-diff branch in `src/awf/runtime/pr_monitor_runner/remote_repair.py` and the focused regression test that already covers the malformed recovered diff.

## Requirements Checklist

- Verify the reported branch exists and is not already cleaned up.
- Extend the malformed recovered-diff regression to require a reset to `operation_start_head`.
- Call the existing recovered missing-HEAD cleanup helper before raising for malformed recovered diff output.
- Keep changes minimal and avoid broad AWF/GitHub-owned validation.

## Implementation Steps

1. Update `test_commit_dirty_worktree_rejects_malformed_recovered_diff` to queue/expect the reset cleanup call.
2. Run that single test and confirm it fails before the production fix.
3. Add `_cleanup_recovered_missing_head_delta(..., reason="recovered_diff_malformed")` in the `ProtectedScopeDiffError` handler.
4. Re-run the single focused test.
5. Record validation evidence in the validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_commit_dirty_worktree_rejects_malformed_recovered_diff -q`

Pass criteria: the focused regression passes after the fix. Full AWF/GitHub validation is intentionally left to AWF after agent completion.

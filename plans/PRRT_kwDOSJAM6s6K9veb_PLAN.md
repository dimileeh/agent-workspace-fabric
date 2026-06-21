# PRRT_kwDOSJAM6s6K9veb Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6K9veb` reports that missing-HEAD dirty
worktree recovery can commit a recovered filesystem snapshot, then block on
protected-scope violations without restoring the worktree branch to the
recovery anchor. Scope is limited to this protected-scope failure path in
`src/awf/runtime/pr_monitor_runner/remote_repair.py`.

## Requirements Checklist

- Verify the reviewed branch is actionable against the current code.
- Add a focused regression test showing protected-scope recovered commits are
  rolled back before `_MonitorPolicyBlockedError` is raised.
- Restore the worktree to `recovery_head` on this protected-scope failure.
- Clean untracked recovered paths introduced by the recovery snapshot.
- Keep changes minimal and avoid broad AWF/GitHub-owned validation.

## Implementation Steps

1. Update the existing missing-HEAD protected-scope unit test to assert cleanup
   commands are issued.
2. Reuse the existing recovery abort cleanup helper in the protected-scope
   branch after `_protected_scope_violations_for_recovered_dirty_commit`
   returns violations.
3. Run the targeted unit test for the changed behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k missing_head_recovery_blocks_recovered_protected_scope`
  - Passes and demonstrates the rollback/cleanup behavior.
- Full AWF/GitHub validation remains managed by AWF after agent completion per
  the workspace contract.

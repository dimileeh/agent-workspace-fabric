# PRRT_kwDOSJAM6s6KzsaC Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6KzsaC` reports that missing-HEAD filesystem
recovery can leave staged recovery residue when the supply-chain policy check
blocks before the recovery commit. Direct pre-push validation has a cleanup
wrapper, and fix-pass `_commit_dirty_worktree` policy failures are already
rolled back, but the shared recovery helper itself still raises after staging.

Scope is limited to cleaning up missing-HEAD filesystem recovery residue on
policy block while preserving the policy exception and adding focused coverage.

## Requirements Checklist

- Verify the review against the current code before changing behavior.
- Add focused regression coverage showing policy-blocked missing-HEAD recovery
  rolls back staged residue before re-raising.
- Keep `_MonitorPolicyBlockedError` propagation unchanged so callers preserve
  the policy reason.
- Keep the change local to the PR monitor missing-HEAD recovery path.
- Run only focused tests for the changed behavior; full AWF/GitHub validation
  remains managed by AWF after agent completion.

## Implementation Steps

1. Update the existing missing-HEAD recovery policy-block test to require a
   cleanup reset before the exception escapes.
2. Change `_recover_missing_head_object_from_filesystem` to hard-reset the
   worktree back to `operation_start_head` when supply-chain policy blocks
   staged recovery paths, logging cleanup failure without clobbering the policy
   exception.
3. Run the targeted unit test for the modified behavior.
4. Record validation evidence in `PRRT_kwDOSJAM6s6KzsaC_VALIDATION.md`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py \
  -k recover_missing_head_object_blocks_policy_before_recovery_commit -q
```

Pass criteria: the targeted regression passes and the policy-block path remains
reason-preserving.

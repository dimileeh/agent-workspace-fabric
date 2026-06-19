# PRRT_kwDOSJAM6s6KyNQY Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6KyNQY` reports that missing-HEAD filesystem
recovery can create and advance a branch commit before supply-chain policy
evaluates the recovered changes. The fix is limited to the PR monitor
missing-HEAD recovery path and focused regression coverage.

## Requirements Checklist

- Evaluate supply-chain policy on staged recovered paths before the recovery
  helper creates a replacement commit.
- Preserve existing post-recovery ownership and protected-scope gates.
- Preserve structured pre-push policy-blocked failure reporting.
- Add focused regression coverage proving a policy block prevents the recovery
  commit.
- Do not run broad AWF/GitHub validation; record focused checks only.

## Implementation Steps

1. Add a pre-commit policy check inside
   `_recover_missing_head_object_from_filesystem`.
2. Pass caller command evidence into the recovery helper from dirty-worktree
   commit and pre-push validation callers.
3. Keep caller-side recovery diff handling for ownership/protected-scope gates,
   without duplicating the policy refresh after the recovery commit.
4. Add or update focused unit tests around policy-blocked missing-HEAD recovery.
5. Run the narrow affected unit test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per workspace contract.

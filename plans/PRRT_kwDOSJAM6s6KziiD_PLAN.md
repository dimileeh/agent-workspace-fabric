# PRRT_kwDOSJAM6s6KziiD Plan

## Problem Statement and Scope

The pre-push missing-HEAD recovery path catches `_MonitorPolicyBlockedError`
from `_recover_missing_head_object_from_filesystem` and returns a retryable
`MONITOR_POLICY_BLOCKED` result. The recovery helper has already reset the
branch/index and staged filesystem recovery with `git add -A` before checking
supply-chain policy. Returning without cleanup can leave staged recovery residue
that the next monitor attempt reports as `PRE_EXISTING_DIRTY_WORKTREE`.

Scope is limited to the policy-blocked missing-HEAD pre-push recovery branch in
`src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and its focused
unit coverage.

## Requirements Checklist

- Add a focused regression proving policy-blocked missing-HEAD recovery cleans
  staged recovery residue before returning `MONITOR_POLICY_BLOCKED`.
- Roll back or clean the recovery residue against `recovery_head` before the
  handler returns the policy-blocked result.
- Preserve the policy-blocked result and message; cleanup failure may be logged
  but must not mask the supply-chain policy reason.
- Keep changes scoped to the reviewed behavior and avoid broad validation.

## Implementation Steps

1. Update the existing pre-push validation edge test for policy-blocked
   missing-HEAD recovery to assert cleanup is invoked with `recovery_head`.
2. In the `_MonitorPolicyBlockedError` handler for missing-HEAD recovery, call
   `_pre_push_validation_cleanup(..., restore_ref=recovery_head)` before
   returning the `MONITOR_POLICY_BLOCKED` result.
3. Log cleanup failure details without changing the returned policy result.
4. Run the targeted unit test covering the changed path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -k missing_head_recovery_policy_block -q`
  - Passes and demonstrates the cleanup call is made.

Full AWF/GitHub validation is intentionally not run inside the agent phase; AWF
owns broad validation and merge gating after completion.

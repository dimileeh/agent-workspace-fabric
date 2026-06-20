# PRRT_kwDOSJAM6s6K8e7Z Plan

## Problem Statement and Scope

The missing-HEAD pre-push validation recovery path catches
`_MonitorPolicyBlockedError` but records the generic
`MONITOR_POLICY_BLOCKED` reason instead of the exception's `reason_code`.
Scope is limited to preserving that reason code and covering the behavior with
a focused regression test.

## Requirements Checklist

- Preserve `_MonitorPolicyBlockedError.reason_code` when recovered HEAD
  filesystem recovery is policy-blocked.
- Keep cleanup behavior for policy-blocked missing-HEAD recovery unchanged.
- Add/update a focused unit regression for the preserved reason code.
- Run only focused local validation; full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Implementation Steps

1. Update the existing missing-HEAD recovery policy-block regression to raise a
   non-default protected-scope reason and expect that reason.
2. Confirm the updated regression fails against the current handler.
3. Change the handler to log and return `exc.reason_code`.
4. Re-run the focused regression.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k missing_head_recovery_policy_block_cleans_residue`
- Pass criterion: the focused test passes and demonstrates the preserved
  protected-scope reason while keeping cleanup assertions intact.

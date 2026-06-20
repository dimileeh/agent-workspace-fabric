# REVIEW_4536367667_DIRTY_FINALIZE_REASON Plan

## Problem Statement and Scope

PR review comment `4536367667` reports that dirty-finalize catches
`_MonitorPolicyBlockedError` but returns the generic
`MONITOR_POLICY_BLOCKED` reason instead of the exception's carried
`reason_code`. This can collapse recovered protected-scope repair failures into
retryable policy blocks.

Scope is limited to dirty-finalize policy-block reason propagation and a
focused regression test.

## Requirements Checklist

- Verify the reviewer claim against current code.
- Add focused regression coverage that fails when a non-default
  `_MonitorPolicyBlockedError.reason_code` is collapsed.
- Preserve default policy-block behavior and existing rollback behavior.
- Return the exception's `reason_code` from dirty-finalize.
- Run only targeted validation for the changed behavior.

## Implementation Steps

1. Update the existing dirty-finalize policy-block regression to cover both the
   default policy reason and `PROTECTED_SCOPE_REPAIR_FAILED`.
2. Run the targeted test and confirm the new protected-scope case fails before
   the implementation change.
3. Change dirty-finalize to return `exc.reason_code`.
4. Re-run the targeted test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_dirty_finalize_mirror_hooks.py::test_pre_push_validation_dirty_finalize_preserves_policy_blocked_exception_reason -q`
  must fail before the implementation change for the protected-scope parameter
  and pass after the fix.

Full AWF/GitHub validation is managed by AWF after agent completion.

# Review 4552714190 Pre-Push Cleanup Reason Plan

## Problem Statement And Scope

The PR monitor pre-push validation path currently returns the raw
`ComposeExecCleanupError.reason_code` as the push failure reason. That bypasses
pre-push validation outcome classification and the infrastructure-failure
early exit used by fix-pass retry logic. Keep the persisted validation-run
reason precise, but normalize the push-blocking result to
`PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED`.

## Requirements Checklist

- Update the cleanup-error regression to require
  `PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED` on the returned push result.
- Preserve the failed validation run's raw compose cleanup reason code.
- Keep the push blocked when cleanup fails.
- Avoid broad AWF/GitHub-owned validation; run only focused tests for the
  changed behavior.

## Implementation Steps

1. Update the focused pre-push validation cleanup-error test and confirm it
   fails against the current implementation.
2. Change the `ComposeExecCleanupError` handler in pre-push validation to return
   the normalized pre-push infrastructure failure reason.
3. Run the focused regression test, plus the narrow pre-push validation test
   module if the targeted test passes.
4. Record validation evidence in the matching validation document.

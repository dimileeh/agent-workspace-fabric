# Comment 4587587225 Plan

## Problem Statement And Scope

Address the review-level feedback on PR comment `issue:4587587225` without
broad validation or GitHub writes. The scoped changes are limited to the
monitor-handoff setup failure reason code and the pre-push validation
reason-code readability comment.

## Requirements Checklist

- Preserve the existing setup-dependency network failure reason code when
  monitor-handoff setup fails for a classified network dependency issue.
- Persist `PR_MONITOR_SETUP_FAILED_REASON_CODE` when monitor-handoff setup
  command failure is not a setup-dependency network failure.
- Add a focused regression test for the non-network setup command failure path.
- Add a brief inline comment explaining why pre-push validation stores a
  validation-run reason code that can differ from `_PrePushValidationResult`.
- Run only targeted tests/checks for the touched behavior; broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a failing unit test in the existing monitor-handoff setup test module
   for a plain non-network setup command failure.
2. Update `monitor_handoff_setup.py` so the fallback reason code is
   `PR_MONITOR_SETUP_FAILED_REASON_CODE`.
3. Add the explanatory comment near `persisted_reason_code` in
   `pre_push_validation.py`.
4. Re-run the targeted unit test module or specific tests.
5. Record validation evidence in `COMMENT_4587587225_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  - Passes after implementation.
- Failing-first evidence: the new targeted test fails before implementation
  because the final workspace event has a generic `SERVICE_STARTUP_FAILURE`
  code instead of the monitor-specific `PR_MONITOR_SETUP_FAILED` code.

## Assumptions/Changes

- Local failing-first evidence showed `_mark_failed` already backfills a generic
  failure-reason code when the caller passes `None`. The fix still keeps the
  review intent: monitor-handoff setup command failures now persist the
  monitor-specific reason code instead of relying on the generic fallback.

# Review 4620209013 Heartbeat Failure Throttle Plan

## Problem Statement And Scope

Review-level comment `issue:4620209013` reports that
`ControlWorker._record_heartbeat_safely()` does not advance its monotonic
throttle timestamp when a heartbeat write fails. During a cold-start database
outage, repeated `run_once()` calls can therefore attempt heartbeat writes and
emit `WORKER_HEARTBEAT_WRITE_FAILED` on every poll cycle until the first
successful write.

Scope is limited to throttling handled heartbeat write failures. The separate
readiness-probe deployment timing observation is advisory deployment/runbook
work, not a code defect in this workspace change.

## Requirements Checklist

- Add a focused regression proving repeated failed heartbeat writes are
  throttled inside the heartbeat write interval.
- Preserve the existing behavior that failures are logged and a later call
  after the write interval retries the heartbeat write.
- Keep changes minimal in `ControlWorker._record_heartbeat_safely()`.
- Run targeted tests for the changed behavior only.

## Implementation Steps

1. Add the failing regression to `tests/unit/control/test_worker_stop.py`.
2. Confirm the regression fails against the current implementation.
3. Update `_record_heartbeat_safely()` to advance the throttle timestamp after
   handled heartbeat write failures.
4. Run the focused regression and adjacent heartbeat throttling tests.
5. Create `plans/REVIEW_4620209013_HEARTBEAT_FAILURE_THROTTLE_VALIDATION.md`
   with focused evidence and note that AWF/GitHub owns broad validation.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py::test_record_heartbeat_safely_throttles_failed_writes -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q -k "record_heartbeat_safely_throttles"`.

Full AWF/GitHub validation is managed by AWF after agent completion.

# PRRT_kwDOSJAM6s6DiR9O Plan

## Problem Statement And Scope

The PR review thread reports that preserved active execution validation salvage can
write a validation-requested event even when no executor can dispatch the recovery.
That event blocks later stale-active cleanup, leaving the workspace stuck.

Scope is limited to `src/awf/control/worker.py` and focused regression coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Preserve existing slot-exhaustion behavior: a configured executor with no current
  slots may leave a validation request pending for a later worker pass.
- Prevent no-executor validation salvage from being recorded as recoverable work
  that cannot run.
- Ensure pre-existing validation-requested salvage events do not block stale-active
  failure in a worker that has no executor.
- Keep the change narrowly scoped and covered by unit tests.

## Implementation Steps

1. Add a failing regression test for a no-executor worker encountering committed
   preserved work that would previously request validation.
2. Add a failing regression test for stale-active cleanup with a pre-existing
   validation-requested salvage event and no executor.
3. Update worker recovery/dispatch logic so no-executor validation salvage is not
   treated as dispatched or blocking.
4. Run the focused tests, then run a narrow worker test surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation_salvage_without_executor or preserved_active_validation_request_without_executor"`
  - Passes after implementation and fails before the worker fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation"`
  - Passes, including the existing slot-exhaustion regression.

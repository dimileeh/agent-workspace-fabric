# PRRT_kwDOSJAM6s6DmnP Plan

## Problem Statement and Scope

Worker-restart validation salvage events can be recorded while a workspace is
`running`, then the executor can advance the same workspace to `validating`
before another worker restart scan. The salvage idempotency and stale-active
cleanup gates must still recognize the existing validation-salvage event so
they do not enqueue duplicate validation recovery or fail a workspace with
validation already in progress.

Scope is limited to status matching for worker-restart validation-salvage event
lookups and focused worker unit coverage.

## Requirements Checklist

- Add a regression test proving a `validating` candidate recognizes a current
  validation-salvage event whose payload was written with
  `workspace_status="running"`.
- Prove stale-active cleanup remains blocked for that same cross-status
  validation-salvage event when validation dispatch is still possible.
- Preserve the existing special handling only for
  `ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED` events, without broadening
  unrelated salvage event matching.
- Run targeted unit validation for the new regression.

## Implementation Steps

1. Add the failing regression test in `tests/unit/control/test_worker.py`.
2. Update `_salvage_workspace_status_values` in `src/awf/control/worker.py` so
   validation-salvage lookups from any active execution status consider all
   active execution status payloads.
3. Run the focused regression test, then record validation evidence in
   `plans/PRRT_kwDOSJAM6s6DmnP_VALIDATION.md`.

## Assumptions/Changes

- A second regression is needed for the exact restart path: after the first
  salvage request was recorded while `running`, the executor can move the row
  to `validating` before the next worker scan. In that case there may be an
  active worker-restart validation operation but no `validating` preservation
  event yet, so recovery and stale-cleanup gates must recognize the active
  validation recovery across active execution statuses when dispatch remains
  possible.

# PRRT_kwDOSJAM6s6Don0H Plan

## Problem Statement and Scope

An unresolved review thread reports that stale-active cleanup can miss a preserved active
execution when the workspace advances from `running` to `validating` after preservation.
The existing preservation lookups are status-exact, so a `validating` candidate does not
see a preservation event recorded while the same active execution was `running`.

Scope is limited to the preservation lookup and stale-active salvage guard behavior in
`src/awf/control/worker.py`, plus focused unit coverage. Preservation recording must
remain scoped enough to avoid suppressing one preservation record per active phase.

## Requirements Checklist

- Add a regression test for a `validating` stale-active candidate with a running-status
  preservation event and validation salvage event from the same active recovery cycle.
- Ensure stale-active failure remains blocked when the active validation salvage can
  continue and the preservation was recorded under another active execution status.
- Preserve existing event-floor protections against stale evidence from older execution
  cycles and operator refreshes.
- Preserve exact-status idempotency for recording preservation events per active phase.
- Run the narrow relevant tests and record validation evidence.

## Implementation Steps

1. Add a failing regression test that reproduces the review scenario.
2. Introduce focused helper logic for preservation lookup status values and active-cycle
   event floors.
3. Update latest-preservation lookups used by recovery/stale guards without changing the
   exact-status recording duplicate check.
4. Run the new test first, then the nearby preservation/stale-active tests.
5. Save validation results in `plans/PRRT_kwDOSJAM6s6Don0H_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "validating_candidate_blocks_stale_failure_with_running_preservation or restart_recovery_records_preservation_once_per_active_phase or validating_candidate_reuses_running_validation_salvage_event or validating_candidate_redispatches_active_running_validation_recovery or preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure"`
  - Passes with the new regression and adjacent preservation/recovery policy tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes without lint regressions.

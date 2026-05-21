# Review PRRT_kwDOSJAM6s6DvuNy Refreshed Status Plan

## Problem Statement

After preserved active validation recovery rewinds a non-running workspace back
to `running`, `_recover_preserved_active_execution` refreshes the ORM workspace
but keeps using the original `_ActiveExecutionCandidate.status`. Downstream
preservation and salvage checks can then query for `validating` or `pushing`
payload statuses while the current workspace status is `running`.

## Scope

- Keep the change limited to preserved active execution recovery.
- Preserve existing validation-requested active-status widening behavior.
- Add a regression test for the rewind fall-through path before changing
  production code.

## Requirements

- [x] Reproduce the stale candidate status problem with a unit test.
- [x] After a committed validation rewind and refresh, use the refreshed
      workspace status for subsequent recovery decisions.
- [x] Do not change branch management or push behavior.
- [x] Validate with the narrow worker unit test target.

## Implementation Steps

1. Add a regression test covering failed dispatch after non-running validation
   rewind with salvage events recorded under `running`.
2. Confirm the new test fails against the current implementation.
3. Update `_recover_preserved_active_execution` to refresh the effective
   candidate status after a committed rewind.
4. Run the targeted unit test and relevant lint/type checks if feasible.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::<test name> -q`
- Pass criteria: the regression test fails before the code change and passes
  after the fix.

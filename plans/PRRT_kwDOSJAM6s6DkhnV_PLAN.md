# PRRT_kwDOSJAM6s6DkhnV Plan

## Problem Statement and Scope

The preserved active execution operator-required salvage writer can leave a
pre-existing pending or running validate/push operation active when a
`validating` or `pushing` workspace is preserved after restart and cannot be
automatically recovered. This plan addresses only that inline review thread in
`src/awf/control/worker.py`.

## Requirements Checklist

- Add a regression test proving operator-required salvage cancels superseded
  pending/running validate/push operations for non-running preserved workspaces.
- Preserve running-workspace operator-required behavior.
- Record cancellation details in the operator-required salvage payload.
- Use the operator-required reason code and refresh requested action when
  cancelling superseded operations from this path.
- Run the narrow worker regression test before and after implementation when
  practical, then run focused validation.

## Implementation Steps

1. Add a parametrized unit test beside existing preserved active salvage tests.
2. Confirm the new test fails against the current implementation.
3. Update `_record_preserved_active_operator_required` to cancel superseded
   active validate/push operations when `candidate.status` is not `running`.
4. Ensure the refresh operation and emitted salvage event include
   `cancelled_active_operations` when any were cancelled.
5. Re-run the targeted regression test and a nearby related test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'operator_required_cancels_superseded_active_operation or preserved_active_salvage_writers_recheck_fresh_execution_claim'`

Pass criteria: targeted tests pass, and no unrelated worktree changes are
introduced.

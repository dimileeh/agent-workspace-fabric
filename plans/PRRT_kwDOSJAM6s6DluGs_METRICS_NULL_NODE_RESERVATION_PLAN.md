# Metrics Null-Node Reservation Plan

## Problem Statement and Scope

The metrics allocation scope undercounts legacy active reservations where both
`resource_reservations.node_id` and `workspaces.node_id` are `NULL`. The
scheduler allocation scope already counts those rows for local capacity gating,
so the capacity dashboard can report more available capacity than the scheduler
will admit.

## Requirements Checklist

- Add a regression test for metrics allocation totals that includes a legacy
  null-node reservation on a null-node workspace.
- Confirm the new test fails against the current metrics predicate when
  practical.
- Update `active_latest_totals_for_metrics_allocation_scope` so null/null
  active reservations are included consistently with scheduler allocation.
- Preserve exclusion of null-workspace reservations explicitly assigned to a
  different node.
- Run the narrowest relevant unit test selection.

## Implementation Steps

1. Add a focused repository unit test next to the scheduler allocation scope
   regression.
2. Run that test before implementation to verify the undercount.
3. Extend the metrics allocation branch in
   `_active_latest_resource_reservation_totals_stmt` to include the null/null
   predicate.
4. Run the focused repository tests for scheduler and metrics allocation scope.
5. Record validation evidence in a matching validation document.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "allocation_scope"`

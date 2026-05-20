# PRRT_kwDOSJAM6s6Dbmn3 Stale Queue Reservation Node Plan

## Problem Statement and Scope

The capacity queue blocked-reason metrics should mirror scheduler capacity
decisions for requested workspaces routed to the local node. The current SQL
only joins a requested workspace to its latest active reservation when the
reservation row also has the local `node_id`, so a stale reservation node can
make blocked-reason counts fall back to default demand and underreport queue
pressure.

Scope is limited to `capacity_queue.blocked_reason_counts` behavior in
`src/awf/service/metrics.py` and its unit regression coverage.

## Requirements Checklist

- Add a regression test where a requested workspace is routed to the local node
  but its latest active reservation still has a different `node_id`.
- Confirm the regression fails before implementation when practical.
- Make blocked-reason counts use the latest active reservation demand for the
  scoped requested workspace regardless of stale reservation `node_id`.
- Preserve existing SQL aggregation behavior and node-scoped workspace
  filtering.
- Validate with targeted unit tests.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_metrics.py`.
2. Run the new test and confirm it fails against the existing join condition.
3. Remove the reservation `node_id` predicate from the blocked-reason join while
   keeping `reservation_rank == 1` and workspace node scope filtering.
4. Run the new regression and nearby metrics tests.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -k "capacity_queue_blocked_reason_counts" -q`

Pass criteria: the capacity queue blocked-reason regression passes, and the
existing SQL aggregation capacity queue tests continue to pass.

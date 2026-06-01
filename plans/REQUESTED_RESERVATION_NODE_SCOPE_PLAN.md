# Requested Reservation Node Scope Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6GQ7Ya` reports that scheduler candidate queries let any named worker list a `requested` workspace whose `workspaces.node_id` is `NULL`, even when an active `resource_reservations.node_id` already reserves the workspace for a different node. The fix is scoped to scheduler candidate selection for node-scoped workers and the associated focused repository regression tests.

## Requirements Checklist

- Requested scheduler candidate queries with a `node_id` must treat the active latest `ResourceReservation.node_id` as the planned placement when `Workspace.node_id` is `NULL`.
- A requested workspace reserved for node A must not be listed for node B.
- A requested workspace reserved for the current node must still be listed before the worker stamps `Workspace.node_id`.
- Existing already-stamped workspace node filtering must keep working.
- Verification must use focused tests only; full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a focused regression test in the workspace repository scheduler tests covering reserved requested rows with null `Workspace.node_id`.
2. Update `_schedulable_workspace_ids_stmt` to apply reservation-aware node filtering for requested rows.
3. Keep non-requested scheduler node filtering behavior scoped to the workspace row.
4. Run the new focused test, then a small neighboring scheduler repository test selection.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_scopes_null_workspace_node_to_active_reservation_node -q`
  - Passes and fails before the implementation when practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_postgres_scheduler_workspace_rows_can_scope_to_node_id tests/unit/db/test_workspace_repository_parts/test_workspace_repository_part_002.py::TestOwnedPathOverlapLookup::test_requested_scheduler_scopes_null_workspace_node_to_active_reservation_node -q`
  - Passes after implementation.

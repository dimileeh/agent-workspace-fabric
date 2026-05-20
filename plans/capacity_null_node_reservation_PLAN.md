# Capacity Null-Node Reservation Plan

## Problem Statement and Scope

A PR review identified that local capacity allocation can undercount legacy
active workspaces when both `workspaces.node_id` and the latest active
`resource_reservations.node_id` are `NULL`. The fix is scoped to the capacity
gate allocation accounting and its unit regression coverage.

## Requirements Checklist

- Add a regression test showing a null-node active workspace with a null-node
  active reservation consumes local capacity.
- Confirm the regression fails against the current implementation when
  practical.
- Update allocation accounting so the mismatched-reservation branch treats
  `Workspace.node_id IS NULL` as local for this legacy coverage path.
- Preserve behavior that remote-node active workspaces remain excluded.
- Commit only the files changed for this review comment.

## Implementation Steps

1. Add a focused worker scheduler test near the existing local capacity tests.
2. Run the new test to confirm it fails before the code change.
3. Update `_add_mismatched_node_active_workspace_reservations` to include
   null-node active workspaces in the outer workspace query.
4. Run the new test and a narrow related test selection.
5. Write a validation document with requirement status and evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node or mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.

## Assumptions/Changes

- The current `ResourceReservation` model and migration make
  `resource_reservations.node_id` non-null, so the regression will use a
  null-node workspace with an active reservation still assigned to another node.
  This is the enforceable form of the same accounting gap and is closed by the
  reviewer's proposed `Workspace.node_id IS NULL` outer-query fix.

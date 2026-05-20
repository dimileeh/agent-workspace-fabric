# Metrics Local Reserved Totals Plan

## Problem Statement And Scope

The resource saturation metrics endpoint counts local workspaces through
workspace routing, but sums persisted reservation totals through
`ResourceReservation.node_id`. If a workspace is reassigned or backfilled and
the latest reservation node differs from `Workspace.node_id`, local capacity
metrics can include remote reservations, miss local reservations, or fall back
to default demand for the wrong workspace.

Scope is limited to local metrics aggregation in `src/awf/service/metrics.py`
and focused regression tests for the metrics API.

## Requirements Checklist

- Local `reserved_resources` totals must derive persisted reservation demand
  from workspaces in `_workspace_node_scope_filter(node_id)`.
- Local `allocated_resources` and `allocated_capacity` must use the same
  workspace-routing scope for allocated statuses.
- Local queued `capacity_queue.planned_resources` must use the same
  workspace-routing scope for requested workspaces.
- Unreserved local workspace fallback/default demand must continue to work.
- Keep repository `active_latest_totals(node_id=...)` semantics unchanged for
  callers that intentionally filter reservation rows by reservation node.
- Add a regression test that fails before implementation and demonstrates a
  mismatched reservation node does not skew local metrics.

## Implementation Steps

1. Add a metrics API regression test with one local-routed workspace whose
   latest reservation names another node, plus one remote-routed workspace
   whose latest reservation names the local node.
2. Confirm the new test fails against the current implementation.
3. Add a local metrics helper that joins latest active reservations to
   `Workspace` and filters by `_workspace_node_scope_filter(node_id)` and
   optional statuses.
4. Use the helper for reserved, allocated, and queued planned metrics totals.
5. Run the focused regression test and relevant metrics capacity test module.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing -q`
  fails before the implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  passes after the implementation.

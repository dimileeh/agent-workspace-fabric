# Metrics Local Reserved Totals Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DbzdN_METRICS_LOCAL_RESERVED_TOTALS_PLAN.md`

## Requirement Status

- Complete: Local `reserved_resources` totals derive persisted reservation
  demand from workspaces in `_workspace_node_scope_filter(node_id)`.
- Complete: Local `allocated_resources` and `allocated_capacity` use the same
  workspace-routing scope for allocated statuses.
- Complete: Local queued `capacity_queue.planned_resources` uses the same
  workspace-routing scope for requested workspaces.
- Complete: Unreserved local workspace fallback/default demand continues to use
  existing workspace-count fallback and defaulted DinD slot logic.
- Complete: Repository `active_latest_totals(node_id=...)` semantics remain
  unchanged; the metrics service now uses its own workspace-scoped aggregate.
- Complete: Added a regression test that fails before implementation and
  demonstrates mismatched reservation node IDs do not skew local metrics.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `tests/unit/api/test_metrics_capacity.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing -q`
  - Before implementation: failed because `reserved_resources` included remote
    reservation totals selected by `ResourceReservation.node_id`.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  - Passed, 12 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/api/test_metrics_capacity.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Remaining Gaps

None.

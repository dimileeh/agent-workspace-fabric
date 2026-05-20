# PR270 Review-Level Node-Scoped Capacity Validation

Plan reference:
`plans/PR270_REVIEW_LEVEL_NODE_SCOPED_CAPACITY_PLAN.md`

## Requirement Status

- Derive local node identifier from `Settings.worker_node_id` with `local`
  fallback: Complete.
- Scope resource saturation status counts, reserved resources, allocated
  resources, defaulted DinD counts, queue depth, oldest queued workspace,
  planned queue demand, and queue blocker counts to the local node: Complete.
- Preserve legacy/unassigned workspace visibility for `NULL` workspace
  `node_id` while excluding explicit sibling-node rows: Complete.
- Ensure reservation aggregation uses latest active reservation and local
  reservation node filtering: Complete.
- Remove `active_workspace_count` from public
  `QueuePlannedResourcesResponse` and generated `openapi.json` while preserving
  `ReservedResourcesResponse.active_workspace_count`: Complete.
- Add or update regressions before implementation: Complete.

## Evidence

Files changed:

- `src/awf/service/metrics.py`
- `src/awf/api/routes/metrics.py`
- `tests/unit/service/test_metrics.py`
- `tests/unit/api/test_metrics_capacity.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`

Regression checkpoint before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_resource_saturation_scopes_capacity_view_to_local_node tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure -q`
- Result: failed as expected on global node aggregation and public queue schema
  mismatch.

Final verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_openapi_artifact.py -q`
  - Result: `113 passed in 62.37s`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Result: `OK: openapi.json matches the current app spec.`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py src/awf/api/routes/metrics.py tests/unit/service/test_metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_openapi_artifact.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: `Success: no issues found in 157 source files`

## Gaps

None.

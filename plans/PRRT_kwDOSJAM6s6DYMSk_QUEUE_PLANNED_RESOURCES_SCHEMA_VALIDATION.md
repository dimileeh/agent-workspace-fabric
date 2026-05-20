# PRRT_kwDOSJAM6s6DYMSk Queue Planned Resources Schema Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DYMSk_QUEUE_PLANNED_RESOURCES_SCHEMA_PLAN.md`

## Requirement Status

- Complete: endpoint regression coverage now proves
  `capacity_queue.planned_resources` serializes only resource totals and omits
  `active_workspace_count`.
- Complete: OpenAPI coverage now proves `planned_resources` references
  `QueuePlannedResourcesResponse` and that schema omits `active_workspace_count`.
- Complete: `reserved_resources` and `allocated_resources` continue to use
  `ReservedResourcesResponse`, which still exposes `active_workspace_count`.
- Complete: `openapi.json` was regenerated from the FastAPI app and now points
  queued planned resources at the dedicated schema.
- Complete: focused API/OpenAPI tests, OpenAPI drift check, lint, and focused
  type check passed.

## Evidence

Files changed:

- `src/awf/api/routes/metrics.py`
- `tests/unit/api/test_metrics_capacity.py`
- `tests/unit/api/test_openapi_artifact.py`
- `openapi.json`
- `plans/PRRT_kwDOSJAM6s6DYMSk_QUEUE_PLANNED_RESOURCES_SCHEMA_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DYMSk_QUEUE_PLANNED_RESOURCES_SCHEMA_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema -q`
  - Result: failed before implementation because the endpoint and OpenAPI
    schema still exposed `active_workspace_count` through
    `ReservedResourcesResponse`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema -q`
  - Result: passed, `2 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
  - Result: passed and rewrote `openapi.json`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure tests/unit/api/test_openapi_artifact.py -q`
  - Result: passed, `19 passed`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/routes/metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_openapi_artifact.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/api/test_openapi_artifact.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/api/routes/metrics.py`
  - Result: passed.

## Gaps

No planned gaps remain.

# PRRT_kwDOSJAM6s6DYMSk Queue Planned Resources Schema Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DYMSk` reports that
`capacity_queue.planned_resources` reuses `ReservedResourcesResponse`, which
includes `active_workspace_count`. That field is meaningful for active
workspace reservations but semantically wrong for queued demand. The scope is
to give queued planned resource totals their own public response schema while
preserving internal capacity accounting.

## Requirements Checklist

- [ ] Add or update regression coverage proving queued planned resources do
      not expose `active_workspace_count`.
- [ ] Add or update OpenAPI coverage proving `planned_resources` references a
      dedicated queue/planned resource schema.
- [ ] Keep `reserved_resources` and `allocated_resources` response contracts
      unchanged.
- [ ] Regenerate `openapi.json` from the current FastAPI app.
- [ ] Validate the focused API/OpenAPI tests and OpenAPI drift check.

## Implementation Steps

1. Update focused endpoint and OpenAPI tests to expect a queue-specific
   planned resource schema without `active_workspace_count`.
2. Add a dedicated `QueuePlannedResourcesResponse` model in the metrics route
   response layer and use it for `CapacityQueueSummaryResponse.planned_resources`.
3. Regenerate `openapi.json` with `scripts/generate_openapi.py`.
4. Run focused tests and `uv run --python 3.12 --extra dev python
   scripts/generate_openapi.py --check`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure tests/unit/api/test_openapi_artifact.py -q`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`

Pass criteria: all commands exit successfully, queued planned resources omit
`active_workspace_count`, and checked-in `openapi.json` matches the generated
spec.

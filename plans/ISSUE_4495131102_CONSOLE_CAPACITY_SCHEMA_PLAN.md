# Issue 4495131102 Console Capacity Schema Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` repeats two concerns for the local capacity
queue metrics surface:

- `_capacity_queue_blocked_reason_counts` should not perform an unbounded ORM
  scan of requested workspaces.
- `capacity_queue.planned_resources` omits `active_workspace_count` in the API
  response while the console TypeScript contract still reuses
  `ReservedResources`, which requires that field.

Current backend tests already prove blocker counts are computed with one SQL
aggregate statement instead of loading requested workspace rows, and the API
schema intentionally uses `QueuePlannedResourcesResponse` without
`active_workspace_count`. This plan treats the backend scan item as already
handled and scopes code changes to the console TypeScript contract mismatch.

## Requirements Checklist

- [ ] Preserve the existing SQL aggregation behavior for capacity queue blocker
      counts and document the regression evidence.
- [ ] Add failing type-level coverage proving console queue planned resources
      accept resource totals without `active_workspace_count`.
- [ ] Add failing type-level coverage proving console queue planned resources do
      not accept `active_workspace_count`.
- [ ] Update console types and normalization so
      `CapacityQueueSummary.planned_resources` matches the API/OpenAPI queue
      schema.
- [ ] Validate focused service/API/OpenAPI regressions and console typecheck.

## Implementation Steps

1. Add a console TypeScript contract file that is included by `tsc --noEmit` and
   fails under the current `ReservedResources` reuse.
2. Run the console typecheck to confirm the expected failure.
3. Introduce a queue-specific planned resource interface in
   `apps/console/lib/types.ts`.
4. Remove the synthetic `active_workspace_count` fallback from console
   saturation normalization.
5. Run focused Python regressions for the reviewed backend concerns, OpenAPI
   drift check, and console typecheck.

## Verification Commands And Pass Criteria

- `npm --prefix apps/console run typecheck`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_aggregates_requested_demands_in_sql tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure -q`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`

Pass criteria: console typecheck passes with queue planned resources excluding
`active_workspace_count`, focused backend/API regressions pass, and the checked-in
OpenAPI artifact remains stable.

## Assumptions/Changes

- In this workspace the system Python environment does not include FastAPI, so
  OpenAPI drift validation is run through the repository `uv` dev environment.

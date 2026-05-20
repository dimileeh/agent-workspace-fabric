# Issue 4495131102 Console Capacity Schema Validation

Plan reference:
`plans/ISSUE_4495131102_CONSOLE_CAPACITY_SCHEMA_PLAN.md`

## Requirement Status

- Complete: Preserved `_capacity_queue_blocked_reason_counts` SQL aggregation
  behavior. Focused regression coverage proves the helper executes one
  aggregate SQL statement and does not load requested workspace ORM rows.
- Complete: Added type-level console coverage proving
  `CapacityQueueSummary.planned_resources` is exactly the queue planned resource
  totals shape without `active_workspace_count`.
- Complete: Updated the console API contract to use a queue-specific planned
  resource interface instead of `ReservedResources`.
- Complete: Removed the dashboard fallback normalization that synthesized
  `active_workspace_count` for queued planned resource totals.
- Complete: Focused service/API/OpenAPI regressions, console typecheck, and
  console lint passed.

## Evidence

Files changed:

- `apps/console/lib/types.ts`
- `apps/console/components/console-dashboard.tsx`
- `apps/console/lib/types-contract.test.ts`
- `plans/ISSUE_4495131102_CONSOLE_CAPACITY_SCHEMA_PLAN.md`
- `plans/ISSUE_4495131102_CONSOLE_CAPACITY_SCHEMA_VALIDATION.md`

Commands run:

- `npm --prefix apps/console run typecheck`
  - Initial pre-install run failed before TypeScript with `next: not found`.
  - After `npm --prefix apps/console ci`, passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_aggregates_requested_demands_in_sql tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema tests/unit/api/test_metrics_capacity.py::test_resource_saturation_endpoint_reports_allocated_capacity_and_queue_pressure -q`
  - Passed: `3 passed`.
- `python scripts/generate_openapi.py --check`
  - Failed before app load because the system Python environment does not have
    FastAPI installed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed: `openapi.json` matches the current app spec.
- `npm --prefix apps/console run lint`
  - Passed.
- `git diff --check`
  - Passed.

## Gaps

None.

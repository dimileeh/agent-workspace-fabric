# Review 4495131102 Metrics Hot Path Plan

## Problem Statement and Scope

Review-level comment `issue:4495131102` reports that the resource saturation
endpoint still performs an unbounded requested-workspace load in the hot metrics
path. The actionable scope is `capacity_queue.blocked_reason_counts` in
`src/awf/service/metrics.py`, specifically `_capacity_queue_candidates`.

The same comment also says `QueuePlannedResourcesResponse` mismatches the
console `ReservedResources` interface by omitting `active_workspace_count`.
Current code has a dedicated console `QueuePlannedResources` type and OpenAPI
contract tests that require the omission, so that claim is treated as stale
unless new local tests prove otherwise.

## Requirements Checklist

- Add regression coverage proving the capacity-queue blocker candidate query is
  bounded with a SQL `LIMIT`.
- Preserve existing blocker-count behavior for queues smaller than the bound,
  including FIFO frontier collapse, latest active reservation semantics,
  default demand fallback, and provider suppression filtering.
- Keep `queued_workspace_count`, `oldest_workspace_id`, and planned queue
  resource totals as whole-queue summaries.
- Preserve the queue-specific API/console contract where planned queue
  resources omit `active_workspace_count`.
- Run focused service/API/console contract validation for the touched behavior.

## Implementation Steps

1. Add or update focused tests in `tests/unit/service/test_metrics.py` so the
   blocker candidate SQL must include a `LIMIT`.
2. Confirm the focused regression fails before implementation.
3. Add a named blocker-scan limit constant in `src/awf/service/metrics.py` and
   apply it to `_capacity_queue_candidates`.
4. Add a short code comment explaining that `blocked_reason_counts` is a bounded
   FIFO diagnostic while the queue totals remain whole-queue aggregates.
5. Run focused tests and static checks.
6. Record requirement-by-requirement validation in
   `plans/REVIEW_4495131102_METRICS_HOT_PATH_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::test_capacity_queue_blocked_reason_counts_loads_latest_requested_demands_once -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_capacity_queue_planned_resources_uses_queue_specific_schema -q`
  passes.
- `npm --prefix apps/console run typecheck`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py tests/unit/api/test_openapi_artifact.py`
  passes.

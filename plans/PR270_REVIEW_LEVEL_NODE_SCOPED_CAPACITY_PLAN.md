# PR270 Review-Level Node-Scoped Capacity Plan

## Problem Statement and Scope

CodeRabbit review comment `4326410507` reports two still-valid issues in the
resource saturation capacity surface:

- The local capacity metrics aggregate workspaces, reservations, requested queue
  demand, queue age, and blocker counts across all nodes instead of the local
  worker node.
- `capacity_queue.planned_resources` exposes `active_workspace_count`, which
  describes active reservations rather than queued demand and creates an API
  contract ambiguity.

Scope is limited to `src/awf/service/metrics.py`, the metrics response schema,
OpenAPI artifact drift, and focused unit/API regressions.

## Requirements Checklist

- Derive the local node identifier from `Settings.worker_node_id`, falling back
  to the stable local service node id `local`.
- Scope resource saturation status counts, reserved resources, allocated
  resources, defaulted DinD counts, queue depth, oldest queued workspace, planned
  queue demand, and queue blocker counts to the local node.
- Preserve legacy/unassigned workspace visibility for workspace rows whose
  `node_id` is `NULL`, while excluding rows explicitly owned by sibling nodes.
- Ensure reservation aggregation uses the latest active reservation and filters
  by local reservation node.
- Remove `active_workspace_count` from the public
  `QueuePlannedResourcesResponse` schema and generated `openapi.json`; keep
  `ReservedResourcesResponse.active_workspace_count`.
- Add or update regression tests before implementing the production changes.

## Implementation Steps

1. Add failing regressions in service/API/OpenAPI tests for sibling-node
   exclusion and queue planned resource schema shape.
2. Update metrics helpers to accept and apply `node_id` filters consistently.
3. Remove `active_workspace_count` from the queue planned resources API model
   and update response expectations.
4. Regenerate `openapi.json` and verify it is stable.
5. Run focused tests first, then lint/type/spec checks appropriate to the
   touched Python/API surface.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_openapi_artifact.py -q`
- `python scripts/generate_openapi.py --check`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py src/awf/api/routes/metrics.py tests/unit/service/test_metrics.py tests/unit/api/test_metrics_capacity.py tests/unit/api/test_openapi_artifact.py`
- `uv run --python 3.12 --extra dev mypy src/awf`

Pass criteria: all commands complete successfully, `openapi.json` matches the
generated spec, and the final diff only addresses the review feedback.

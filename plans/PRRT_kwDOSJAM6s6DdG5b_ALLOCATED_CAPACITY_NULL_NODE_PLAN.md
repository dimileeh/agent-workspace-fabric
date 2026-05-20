# PRRT_kwDOSJAM6s6DdG5b Allocated Capacity Null Node Plan

## Problem Statement And Scope

The review thread reports that `summarize_resource_saturation` overcounts local
allocated capacity when a workspace has `Workspace.node_id IS NULL` but its
latest active reservation names another worker. The scheduler does not enforce
that reservation against the local worker, so allocated capacity metrics and
capacity queue blocker counts must mirror the scheduler baseline.

Scope is limited to allocated-capacity metrics and blocker counts for
`src/awf/service/metrics.py`. Generic workspace-scoped reserved/planned
reservation totals should keep their existing behavior.

## Requirements Checklist

- Add a regression showing a null-node active workspace with a non-local latest
  reservation is excluded from local `allocated_resources`.
- Show that the same excluded allocation does not create local capacity queue
  blockers.
- Preserve current behavior for local workspaces whose latest reservation still
  names a prior node.
- Preserve existing reserved/planned workspace-scoped totals.
- Commit only the files changed for this thread.

## Implementation Steps

1. Add a focused regression in `tests/unit/service/test_metrics.py`.
2. Confirm the regression fails against the current implementation.
3. Add an allocated-capacity totals helper in `src/awf/service/metrics.py` that
   mirrors scheduler allocation rules:
   - include latest active reservations whose reservation `node_id` is local;
   - include latest active reservations for local workspace rows even if the
     reservation names a prior node;
   - exclude `Workspace.node_id IS NULL` rows when the latest reservation names
     a non-local node;
   - continue counting unreserved local/null active workspaces via defaults.
4. Route `_allocated_resources_for_session` through the new helper without
   changing reserved/planned totals.
5. Run focused tests and the relevant lint/type checks if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py::<new regression> -q`
  must fail before implementation and pass after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/metrics.py tests/unit/service/test_metrics.py`
  must pass.

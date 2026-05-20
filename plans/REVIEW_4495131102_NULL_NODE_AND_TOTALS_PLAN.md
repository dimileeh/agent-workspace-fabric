# Review 4495131102 Null-Node And Totals Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` reports two remaining local-capacity
scheduler issues:

- scheduler allocation can count a workspace whose `Workspace.node_id` and
  latest reservation `node_id` are both `NULL` on every worker node;
- metrics duplicates the latest-active reservation window-function totals query
  instead of using the repository-owned helper.

Scope is limited to worker allocation accounting, repository reservation totals
helpers, metrics delegation, and focused regression tests.

## Requirements Checklist

- Add/update regression coverage showing null-node/null-reservation rows are not
  attributed to every explicit worker node.
- Preserve counting for reservations already attributed to the local node.
- Preserve counting for local workspace rows whose latest reservation still
  names a prior node.
- Move scheduler allocation totals aggregation into
  `ResourceReservationRepository`.
- Update metrics to delegate scheduler allocation totals to the repository.
- Run focused worker, repository, and metrics tests.

## Implementation Steps

1. Update the worker null-node regression to expect ambiguous null/null rows to
   be skipped for an explicit worker node.
2. Add a metrics delegation regression for scheduler allocation totals.
3. Add a repository regression for scheduler allocation scope semantics.
4. Implement the worker guard and repository totals helper.
5. Replace the duplicated metrics SQL with repository delegation.
6. Run the focused tests and static checks practical for the touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node_workspace_with_null_node_reservation or mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q -k "active_latest_totals_for_scheduler_allocation_scope"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/db/test_scheduler_records.py tests/unit/api/test_metrics_capacity.py`
  passes.

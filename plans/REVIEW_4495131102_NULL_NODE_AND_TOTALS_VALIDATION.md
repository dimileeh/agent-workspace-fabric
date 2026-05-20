# Review 4495131102 Null-Node And Totals Validation

Plan reference: `plans/REVIEW_4495131102_NULL_NODE_AND_TOTALS_PLAN.md`

## Requirement Status

- Add/update regression coverage showing null-node/null-reservation rows are not
  attributed to every explicit worker node: Complete.
  Evidence: `tests/unit/control/test_worker.py` now expects the explicit-node
  allocation helper to skip a null-node workspace when the latest reservation is
  also node-unaware.
- Preserve counting for reservations already attributed to the local node:
  Complete.
  Evidence: `tests/unit/db/test_scheduler_records.py` covers a null-routed
  workspace whose reservation names `node-a`.
- Preserve counting for local workspace rows whose latest reservation still
  names a prior node: Complete.
  Evidence: worker and repository regressions include local workspace rows with
  non-local reservation nodes.
- Move scheduler allocation totals aggregation into
  `ResourceReservationRepository`: Complete.
  Evidence: `active_latest_totals_for_scheduler_allocation_scope` is implemented
  in `src/awf/db/repositories.py`.
- Update metrics to delegate scheduler allocation totals to the repository:
  Complete.
  Evidence: `tests/unit/api/test_metrics_capacity.py` fails if metrics executes
  local SQL for scheduler allocation totals.
- Run focused worker, repository, and metrics tests: Complete.

## Verification Evidence

Expected failing checks before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node_workspace_with_null_node_reservation or mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes"`
  failed on `allocated.workspace_count == 1`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q -k "active_latest_totals_for_scheduler_allocation_scope"`
  failed because metrics executed SQL instead of delegating.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  failed because the repository method was missing.

Passing checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "null_node_workspace_with_null_node_reservation or mismatched_reservation_node or ignores_allocated_capacity_on_other_nodes"`
  passed: `3 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q -k "active_latest_totals_for_scheduler_allocation_scope"`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q -k "scheduler_allocation_scope"`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "allocated_capacity_matches_scheduler_null_node_rules or capacity_queue_blocked_reason_counts"`
  passed: `4 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_scheduler_records.py -q`
  passed: `10 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/db/test_scheduler_records.py tests/unit/api/test_metrics_capacity.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
- `git diff --check`
  passed.

## Gaps

No known gaps remain.

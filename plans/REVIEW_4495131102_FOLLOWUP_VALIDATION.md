# Review 4495131102 Follow-Up Validation

Plan reference: `plans/REVIEW_4495131102_FOLLOWUP_PLAN.md`

## Requirement Status

- Complete: Documented that a provider circuit opening between polls can reuse
  the requested-capacity resume cursor for one scheduler cycle, and that
  observed suppression expiry resets the cursor later.
- Complete: Validated that active local workspaces with stale/different
  reservation `node_id` values are already counted through scheduler allocation
  scope.
- Complete: Preserved the existing policy that a null-node workspace with an
  active reservation owned by another node is not default-counted as local
  capacity.
- Complete: Documented that `capacity_queue.blocked_reason_counts` reports
  distinct FIFO saturation frontiers, not every queued workspace behind a
  frontier.
- Complete: Ran focused tests covering the protected worker and metrics
  semantics.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/service/metrics.py`
- `plans/REVIEW_4495131102_FOLLOWUP_PLAN.md`
- `plans/REVIEW_4495131102_FOLLOWUP_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node or null_node_workspace_with_null_node_reservation or capacity_gate_unreserved_defaults_use_deduplicated_join"`
  - Passed: `3 passed, 215 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts_accumulates_fifo_demands or capacity_queue_blocked_reason_counts_collapses_fifo_deferred_frontier"`
  - Passed: `2 passed, 88 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/service/test_metrics.py`
  - Passed.

## Remaining Gaps

None.

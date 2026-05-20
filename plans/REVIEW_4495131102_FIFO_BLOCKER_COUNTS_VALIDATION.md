# Review 4495131102 FIFO Blocker Counts Validation

Plan reference: `plans/REVIEW_4495131102_FIFO_BLOCKER_COUNTS_PLAN.md`

## Requirement Status

- Complete: Keep capacity-enabled `run_once` on the advisory-locked capacity
  claim path without pre-listing requested IDs.
  - Evidence:
    `test_requested_capacity_gate_claims_without_prefetching_requested_ids`
    passed.
- Complete: Make `_list_requested` return only the non-capacity candidate
  limit.
  - Evidence:
    `test_list_requested_uses_non_capacity_limit_when_called_directly` failed
    before implementation with limit `4` instead of `1`, then passed.
- Complete: Preserve explicit-limit-only capacity blocker behavior in metrics.
  - Evidence:
    `test_capacity_queue_blocked_reason_counts_ignores_detected_cpu_and_memory_limits`
    passed.
- Complete: Preserve stale reservation-node demand handling for requested
  workspaces.
  - Evidence:
    `test_capacity_queue_blocked_reason_counts_uses_stale_node_reservation_demand`
    passed.
- Complete: Count queue blockers against accumulated capacity in scheduler
  order.
  - Evidence:
    `test_capacity_queue_blocked_reason_counts_accumulates_fifo_demands`
    failed before implementation with `{}` and passed with
    `{"DIND_CAPACITY_SATURATED": 1}`.
- Complete: Add a regression where an earlier queued workspace consumes
  capacity and a later workspace is counted as blocked only after FIFO
  accumulation.
  - Evidence:
    `test_capacity_queue_blocked_reason_counts_accumulates_fifo_demands`
    covers this case.

## Commands Run

- Expected failing pre-implementation worker check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_claims_without_prefetching_requested_ids or list_requested_uses_non_capacity_limit"`
  - Result before implementation: failed on `_list_requested` passing expanded
    limit `4`.
- Expected failing pre-implementation metrics check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts_accumulates_fifo_demands or capacity_queue_blocked_reason_counts_collapses_fifo_deferred_frontier"`
  - Result before implementation: failed on FIFO accumulation under-count and
    repeated deferred-frontier over-count.
- Focused worker check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_claims_without_prefetching_requested_ids or list_requested_uses_non_capacity_limit"`
  - Result: passed, `2 passed, 210 deselected`.
- Focused metrics check:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k capacity_queue_blocked_reason_counts`
  - Result: passed, `5 passed, 83 deselected`.
- Static checks:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/service/test_metrics.py`
  - Result: passed.
- Type checks:
  `uv run --python 3.12 --extra dev mypy src/awf`
  - Result: passed.
- Whitespace sanity:
  `git diff --check`
  - Result: passed.

## Additional Probe

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_metrics_capacity.py -q`
  - Result: failed in
    `test_resource_saturation_endpoint_scopes_reservations_by_workspace_routing`
    before any capacity-queue assertion because `allocated_resources` includes
    a mismatched-node active reservation. This is outside the review comment
    fixed here and separate from the changed `capacity_queue` blocker path.

## Remaining Gaps

None for the saved plan.

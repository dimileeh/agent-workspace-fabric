# Review 4495131102 Follow-Up Plan

## Problem Statement and Scope

Review-level comment `issue:4495131102` flagged three remaining observations in
the PR #270 local capacity scheduler work:

- requested-capacity resume cursors may survive when a provider circuit breaker
  opens between polls;
- `_add_unreserved_active_workspace_defaults` excludes active workspaces with
  any active reservation from default-demand fallback accounting;
- capacity queue blocker counts intentionally collapse repeated FIFO saturation
  boundaries by reason code.

Scope is limited to `src/awf/control/worker.py`, `src/awf/service/metrics.py`,
focused regression validation, and this plan/validation pair. No GitHub writes,
pushes, branch changes, or unrelated refactors.

## Requirements Checklist

- Document that a provider circuit breaker opening between requested-capacity
  polls may leave a reused resume cursor in place for at most one scheduler
  cycle, and that the cursor is invalidated once an observed suppression expiry
  is stored and elapses.
- Validate that active local workspaces with stale/different reservation
  `node_id` values are already counted through scheduler allocation scope rather
  than fallback defaults.
- Preserve the existing policy that a null-node workspace with an active
  reservation owned by another node is not default-counted as local capacity.
- Document that `capacity_queue.blocked_reason_counts` reports distinct FIFO
  saturation frontiers, not every queued workspace behind a frontier.
- Run focused tests covering the protected scheduler and metrics semantics.

## Implementation Steps

1. Add concise code comments near the resume-cursor reuse check and metrics
   `deferred_frontiers` set.
2. Leave `_active_reservation_workspace_ids_subquery` behavior unchanged unless
   focused tests expose a real undercount.
3. Run targeted worker tests for mismatched reservation node and null-node
   reservation ownership.
4. Run targeted metrics tests for collapsed deferred frontiers and related
   blocked-reason counting.
5. Record validation evidence in
   `plans/REVIEW_4495131102_FOLLOWUP_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "mismatched_reservation_node or null_node_workspace_with_null_node_reservation or capacity_gate_unreserved_defaults_use_deduplicated_join"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k "capacity_queue_blocked_reason_counts_accumulates_fifo_demands or capacity_queue_blocked_reason_counts_collapses_fifo_deferred_frontier"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/service/test_metrics.py`
  passes.

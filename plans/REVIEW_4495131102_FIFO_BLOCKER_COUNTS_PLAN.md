# Review 4495131102 FIFO Blocker Counts Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4495131102`.

Scope is limited to:

- removing the dead capacity branch from `ControlWorker._list_requested`;
- making `capacity_queue.blocked_reason_counts` model requested workspaces in
  scheduler order instead of checking every queued workspace against one fixed
  allocation snapshot;
- focused tests and validation docs for those changes.

## Requirements Checklist

- [ ] Keep capacity-enabled `run_once` on the advisory-locked capacity claim
  path without pre-listing requested IDs.
- [ ] Make `_list_requested` return only the non-capacity candidate limit.
- [ ] Preserve explicit-limit-only capacity blocker behavior in metrics.
- [ ] Preserve stale reservation-node demand handling for requested workspaces.
- [ ] Count queue blockers against accumulated capacity in scheduler order.
- [ ] Add a regression where an earlier queued workspace consumes capacity and
  a later workspace is counted as blocked only after that FIFO accumulation.

## Implementation Steps

1. Add focused regression tests for `_list_requested` and FIFO-modeled blocker
   counts.
2. Simplify `_list_requested` to use `max_concurrent_provisions`.
3. Refactor metrics blocker counting to load requested demands once, order them
   by scheduler score, and accumulate admitted demand before evaluating later
   candidates.
4. Run the focused tests, then lint/type checks relevant to the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_gate_claims_without_prefetching_requested_ids or list_requested_uses_non_capacity_limit"`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_metrics.py -q -k capacity_queue_blocked_reason_counts`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/service/metrics.py tests/unit/control/test_worker.py tests/unit/service/test_metrics.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.

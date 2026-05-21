# Capacity Cursor Age Refresh Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6DkH6H` reports that `_claim_requested_ids_with_capacity`
can reuse a capacity resume cursor with the cursor's old `scoring_at`. Because
scheduler effective score includes age boost, unchanged allocation and queue
signatures do not guarantee scheduler order is still valid across polls.

Scope is limited to requested-workspace local-capacity resume behavior in
`src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test showing a workspace that crosses an age-boost threshold
  between capacity polls is not delayed behind a stale bounded-scan cursor.
- Keep bounded capacity resume behavior when queue content, allocation, provider
  suppression, and scheduler age buckets are unchanged.
- Reset the inter-poll requested-capacity cursor when any requested candidate for
  the worker node crosses a scheduler age-boost threshold since the cursor was
  recorded.
- Preserve existing queue/allocation/provider-suppression cursor invalidation
  behavior.
- Run the narrow regression first to confirm failure, then run targeted passing
  tests after implementation.

## Implementation Steps

1. Add a unit test under requested capacity gate coverage that freezes worker
   time, creates blocked higher-base-priority candidates plus an older fitting
   candidate that ages into priority on the next poll, and asserts the second
   poll claims the fitting candidate.
2. Implement a helper that detects requested queue age-bucket changes between
   the saved cursor `scoring_at` and the current decision time.
3. Gate capacity cursor reuse on the helper result in
   `_claim_requested_ids_with_capacity`.
4. Re-run the failing regression and nearby requested-capacity cursor tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "age_boost_threshold or requested_capacity_gate_resumes_after_bounded_blocked_scan or requested_capacity_gate_resets_resume_cursor_when_requested_queue_changes"`
  passes after implementation.
- If practical, run `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`.

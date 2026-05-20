# Review 4495131102 Cursor Composition And UI Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` calls out two remaining capacity-scheduling
follow-ups:

- `_requested_capacity_queue_signature` can keep a stale resume cursor for one
  worker cycle when queue count, maximum timestamps, and maximum workspace id
  remain stable while queue composition changes.
- The console resource panel always renders the `Oldest queued` fact, even when
  the capacity queue is empty and the value is only a dash.

Scope is limited to the requested-queue signature helper, focused worker unit
coverage for the composition-change edge case, and the console capacity panel
rendering condition.

## Requirements Checklist

- Add a regression test proving same-count requested queue replacement changes
  the queue signature even when max `updated_at`, max `created_at`, and max id
  are unchanged.
- Update `_RequestedCapacityQueueSignature` so a queue composition change
  invalidates a saved resume cursor in that edge case.
- Gate the console `Oldest queued` fact so it only appears when a queued
  workspace is actually waiting.
- Keep changes scoped to the cited worker and console areas.

## Implementation Steps

1. Add a failing worker unit test for a requested queue replacement with stable
   count, max timestamps, and max id.
2. Implement a stable queue-composition fingerprint in
   `_requested_capacity_queue_signature`.
3. Gate `Oldest queued` in `console-dashboard.tsx` on queued count or non-null
   wait duration.
4. Run focused worker and console validation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  passes.
- `npm --prefix apps/console run lint` passes for the console change, unless an
  unrelated pre-existing lint failure is documented.
- `npm --prefix apps/console run typecheck` passes for the console change,
  unless an unrelated pre-existing type failure is documented.

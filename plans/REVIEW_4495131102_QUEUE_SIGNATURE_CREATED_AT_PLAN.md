# Review 4495131102 Queue Signature Created-At Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` calls out two capacity scheduler follow-ups:

- `_ordered_queue_decision_matches` intentionally treats a same-attempt
  `LOCAL_CAPACITY_RESERVATION_DEFAULTED` decision as the ordered provisioning
  record, but the code does not explain why no `decided_at` equality check is
  required in that branch.
- `_requested_capacity_queue_signature` can reuse a resume cursor for one poll
  when the requested queue count and max updated timestamp stay constant and
  replacement workspace ids are lexicographically lower than the previous max.

Scope is limited to the worker queue-signature/dedupe helpers and focused unit
coverage for the cursor invalidation edge case.

## Requirements Checklist

- Add a clarifying comment for defaulted-reservation ordered decision dedupe.
- Include `MAX(created_at)` in the requested queue signature so replacements
  with newer creation time invalidate the resume cursor even when count,
  `MAX(updated_at)`, and `MAX(id)` are unchanged.
- Add a regression test that fails against the old three-field signature.
- Keep the change local to worker scheduling behavior.

## Implementation Steps

1. Add a failing unit test around `_requested_capacity_queue_signature` that
   replaces requested rows with the same count, no newer `updated_at`, and
   lexicographically lower ids but newer `created_at`.
2. Update `_RequestedCapacityQueueSignature` and
   `_requested_capacity_queue_signature` to carry max created time.
3. Add the explanatory comment in `_ordered_queue_decision_matches`.
4. Run the narrow unit test, then the relevant worker unit surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k requested_capacity_queue_signature`
  must pass after initially failing before implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  should pass for the touched worker surface.

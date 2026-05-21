# PRRT_kwDOSJAM6s6Dimt Resume Queue Fields Plan

## Problem Statement And Scope

The requested-capacity resume cursor signature can stay unchanged when a queued
workspace's scheduler policy changes but the requested row count, max
timestamps, max id, and requested-id set remain unchanged. That can let a worker
reuse `resume_after` and skip newly urgent candidates until an unrelated queue
or allocation mutation occurs.

Scope is limited to the requested-capacity queue signature and focused worker
regression coverage for the review thread.

## Requirements Checklist

- Add a regression test that fails when a requested workspace's scheduling
  policy changes while the existing aggregate signature fields remain stable.
- Include queue ordering/filter fields in the requested-capacity queue digest so
  `resume_after` is invalidated when those fields mutate.
- Keep both PostgreSQL and non-PostgreSQL signature paths behaviorally aligned.
- Preserve the existing signature tuple shape used by the worker resume state.
- Run the narrow worker test(s) that prove the regression and fix.

## Implementation Steps

1. Add a unit test in `tests/unit/control/test_worker.py` around
   `_requested_capacity_queue_signature`.
2. Confirm the new test fails against the current ID-only digest.
3. Update `_requested_capacity_queue_signature` in `src/awf/control/worker.py`
   so the digest includes per-row queue fields used for requested scheduling and
   provider filtering.
4. Re-run the focused test(s), then run a narrow worker test selection covering
   the existing requested-capacity signature cases.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "requested_capacity_queue_signature"`
  must pass after the fix.
- The newly added regression must fail before the implementation change when
  practical.

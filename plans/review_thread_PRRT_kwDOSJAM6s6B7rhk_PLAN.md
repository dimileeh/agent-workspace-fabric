# Review Thread PRRT_kwDOSJAM6s6B7rhk Plan

## Problem Statement And Scope

The unresolved PR review thread reports that failure epoch reset detection uses
lexical `WorkspaceEvent.id` ordering as a same-timestamp tiebreaker even though
event IDs are random uuid4-derived strings. The scope is limited to failure
causality epoch-boundary handling in `src/awf/service/failure_causality.py` and
focused regression coverage.

## Requirements Checklist

- Add regression coverage proving a same-timestamp epoch reset is detected even
  when the reset event ID sorts lower than the failed event ID.
- Add regression coverage proving stale embedded primary failure evidence is
  ignored for a same-timestamp reset regardless of event ID lexical order.
- Remove failure-causality epoch-boundary comparisons that treat random event
  IDs as chronological ordering evidence.
- Preserve existing failure causality behavior for non-reset events and current
  epoch validation snapshots.
- Keep the change scoped and avoid schema changes unless the existing model
  already exposes a monotonic event ordering key.

## Implementation Steps

1. Update same-timestamp tests in `tests/unit/service/test_failure_causality.py`
   so the reset event ID sorts lower than the failed event ID.
2. Confirm the updated regression fails with the current implementation.
3. Update `src/awf/service/failure_causality.py` to treat same-timestamp reset
   events as epoch boundaries instead of comparing random IDs.
4. Run the targeted failure-causality tests and broader checks as practical.
5. Write validation evidence in
   `plans/review_thread_PRRT_kwDOSJAM6s6B7rhk_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf tests` should pass for
  the touched Python surface.

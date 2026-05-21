# PRRT_kwDOSJAM6s6DX8TS Capacity Deferred Dedupe Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DX8TS` reports that local capacity admission
records a `QUEUE_DECISION_DEFERRED` row every time the worker polls a requested
workspace that is still blocked by the same local capacity constraints. The
scope is to prevent repeated unchanged capacity deferral records while
preserving the first durable reason-code record and preserving new records when
the blocking capacity state changes.

## Requirements Checklist

- [ ] Keep recording the first local capacity deferral for a blocked requested
      workspace.
- [ ] Do not append a new deferred capacity decision when the latest decision
      for the same attempt has the same decision, reason code, and capacity
      blocker signature.
- [ ] Keep recording distinct capacity deferrals when the reason code or
      blocker details change.
- [ ] Keep ordered/defaulted capacity decisions and non-capacity queue decision
      behavior unchanged.
- [ ] Add focused regression coverage for repeated unchanged capacity deferrals.
- [ ] Validate the focused worker test and static checks for touched code.

## Implementation Steps

1. Add a failing worker regression test that runs capacity admission twice for
   the same blocked requested workspace and asserts only one deferred capacity
   decision is stored.
2. Add a small capacity-deferral duplicate predicate in
   `src/awf/control/worker.py` that compares the latest queue decision against
   the current capacity blocker signature.
3. Use the predicate in `_record_capacity_queue_decision` before creating a new
   deferred capacity record.
4. Run the focused regression test, then run lint/type checks for the touched
   worker and test files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k repeated_unchanged_capacity_deferral`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`

Pass criteria: the focused regression fails before implementation, passes after
implementation, and lint/type checks exit successfully.

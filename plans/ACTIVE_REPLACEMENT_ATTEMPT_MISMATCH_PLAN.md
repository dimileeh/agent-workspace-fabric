# Active Replacement Attempt Mismatch Plan

## Problem Statement and Scope

PR thread `PRRT_kwDOSJAM6s6DvuQZ` reports that `_create_preserved_active_replacement`
silently returns when the preserved active workspace's current task attempt is missing
or does not match the attempt id captured earlier by `_recover_preserved_active_execution`.
That leaves no durable salvage outcome for operators or later stale-active scans.

Scope is limited to the preserved-active replacement path in `src/awf/control/worker.py`
and a regression test in the existing worker unit test suite.

## Requirements Checklist

- Add a regression test that fails when the replacement attempt mismatch/missing guard
  exits without a durable salvage event.
- Keep the existing idempotency guard: do not create a replacement when the current
  source attempt no longer matches the captured attempt id.
- Record an explicit salvage outcome and log entry for the mismatch/missing-attempt path.
- Let stale-active failure proceed after the unrecoverable mismatch is recorded instead
  of returning `True` forever from preserved-active recovery.
- Preserve existing replacement behavior for the happy path and existing tests.

## Implementation Steps

1. Add a focused unit test that invokes `_create_preserved_active_replacement` with a
   stale captured attempt id and asserts an `ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE`
   event plus a warning log.
2. Change `_create_preserved_active_replacement` to return a boolean recovery outcome.
3. On missing/mismatched original attempt, write a `salvage_not_possible` event in the
   same locked session and return `False`.
4. Update the caller in `_recover_preserved_active_execution` to return the boolean
   from `_create_preserved_active_replacement`.
5. Run the narrow regression test, then run the touched worker test module if feasible.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k replacement_attempt`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes or any unrelated environmental failure is documented.

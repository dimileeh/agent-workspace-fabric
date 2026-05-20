# PRRT_kwDOSJAM6s6DkIgc Plan

## Problem Statement And Scope

The review thread reports that stale-active cleanup can fail a preserved active
workspace after grace expires when validation salvage has already been requested
but all execution slots are currently occupied. Existing regressions preserve
cleanup when validation recovery is truly undispatchable, such as no executor or
zero configured execution slots.

Scope is limited to preserved-active validation recovery and stale-failure
gating in `src/awf/control/worker.py`, with focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Block stale-active failure while a current validation salvage request is
  pending and this worker has configured execution capacity, even when slots are
  temporarily full.
- Keep no-executor and zero-configured-slot behavior unchanged so
  undispatchable validation recovery does not permanently block cleanup.
- Avoid duplicate salvage side effects while a pending validation recovery is
  waiting for capacity.
- Keep changes narrow and covered by focused unit tests.

## Implementation Steps

1. Add a regression test for a pending validation salvage request with
   `max_concurrent_executions > 0` and all execution slots occupied.
2. Confirm the new test fails on the current implementation.
3. Add a small worker helper for configured execution capacity and use it in the
   validation-salvage redispatch and stale-failure gate.
4. Run the focused validation-salvage tests plus lint on touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_busy_worker_blocks_stale_failure_after_grace -q`
  - Fails before the worker fix and passes afterward.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation_slot_exhaustion or validation_busy_worker_blocks_stale_failure or validation_salvage_without_executor or rewound_validation_salvage_waits_without_duplicate_when_slots_full"`
  - Passes and proves the zero-slot/no-executor behavior remains intact.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Passes.

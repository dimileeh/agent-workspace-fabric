# PRRT_kwDOSJAM6s6Dpkh6 Validation Executor Unavailable Plan

## Problem Statement and Scope

An unresolved PR review thread reports that preserved committed work can be failed after
`ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED` if a later worker cycle has no executor.
The fix is scoped to stale active execution recovery in `src/awf/control/worker.py` and
the targeted regression coverage in `tests/unit/control/test_worker.py`.

## Requirements Checklist

- Reproduce the missing guard with a regression test for validation salvage plus an
  executor-less worker.
- When validation salvage exists and no executor is available, record
  `workspace.active_execution_salvage_blocked` with
  `blocked_reason == "validation_executor_unavailable"`.
- Return `True` from preserved active recovery in that condition so stale active cleanup
  does not fail the workspace in the same cycle.
- Preserve existing behavior for dispatchable validation recovery and slot-exhaustion cases.
- Commit only the files changed for this review thread.

## Implementation Steps

1. Update the existing no-executor validation salvage test to assert blocked salvage
   recovery instead of stale failure.
2. Run that narrow test and confirm it fails on the current implementation.
3. Update `_recover_preserved_active_execution` to write blocked salvage and return `True`
   when an existing validation request cannot be dispatched because `self._executor` is
   `None`.
4. Run the targeted unit tests around validation salvage recovery.
5. Run focused lint for changed files and document validation results.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_validation_salvage_without_executor or preserved_active_committed_work_without_executor or preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure or preserved_active_validation_busy_worker_blocks_stale_failure_after_grace" -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passes.

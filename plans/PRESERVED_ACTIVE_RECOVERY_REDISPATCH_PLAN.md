# Preserved Active Recovery Redispatch Plan

## Problem Statement and Scope

An unresolved PR review thread reports a livelock in preserved active execution
recovery. When a workspace is already in `validating` or `pushing` and has an
active `worker_restart` validation-recovery operation, the worker redispatches
executor work without rewinding the workspace to `running`. Executor recovery
claims now intentionally only match `running`, so this redispatch can no-op while
the worker reports recovery success.

Scope is limited to `ControlWorker` preserved active execution recovery and the
unit regression for the reported non-running redispatch path.

## Requirements Checklist

- Preserve the existing running-workspace redispatch behavior.
- For `validating` and `pushing` workspaces with active `worker_restart`
  validation recovery, persist a transition back to `running` before executor
  redispatch.
- Keep the recovery operation and existing salvage event lineage intact.
- Do not weaken existing stale-cleanup, executor-claim, or safety regression
  tests.
- Verify with a focused failing-then-passing unit test and run the narrow worker
  test selection.

## Implementation Steps

1. Add a regression assertion covering a non-running preserved recovery
   redispatch and proving the workspace status is persisted as `running`.
2. Confirm the regression fails against the current implementation.
3. Update `_recover_preserved_active_execution` so active validation recovery for
   non-running candidates rewinds to `running` before dispatching executor work.
4. Run focused tests for the affected worker and executor recovery paths.
5. Create a validation document with requirement-by-requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "redispatches_active_validation_recovery"`
  must fail before the implementation and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/control/test_executor.py -q -k "worker_restart_recovery or redispatches_active_validation_recovery"`
  must pass after implementation.

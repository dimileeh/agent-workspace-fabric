# PRRT_kwDOSJAM6s6DaOTt Preserved Validation Recovery Plan

## Problem Statement And Scope

The PR review thread reports that preserved active execution salvage requests
validation recovery for clean committed work while leaving `validating` and
`pushing` workspaces in statuses the executor cannot claim. The scope is the
worker restart salvage path for clean committed work and the minimal lifecycle
transition needed for the executor to claim and run validation recovery.

## Requirements Checklist

- Reproduce the stranded `validating`/`pushing` clean committed salvage path with
  focused regression coverage.
- Preserve the existing `running` salvage path.
- Move non-running active execution salvage into an executor-claimable recovery
  status before dispatch.
- Record explicit state-change evidence and keep validation salvage idempotent.
- Avoid stale-active cleanup for recoverable committed work.

## Implementation Steps

1. Add failing worker/state-machine tests for preserved `validating` and
   `pushing` clean committed work.
2. Add the explicit recovery transition edge needed for active execution salvage.
3. Update `_request_preserved_active_validation` to transition non-running
   preserved workspaces to `running` with the salvage payload before dispatch.
4. Run focused tests, then a narrow control-plane validation set.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py -q -k "active_execution_salvage or preserved_active_clean_committed"
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/control/state_machine.py tests/unit/control/test_worker.py tests/unit/control/test_state_machine.py
```

Pass criteria: the new regressions fail before implementation and pass after;
existing preserved-active recovery tests remain green.

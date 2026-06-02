# PRRT_kwDOSJAM6s6GUunb Terminal Runtime Resume Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6GUunb` reports that
`_record_terminal_runtime_released` records and commits
`workspace.terminal_runtime_released`, then calls the planning-scope auto-retry
resume hook. If that resume hook raises, the release scan treats the already
recorded release as failed and the blocked auto-retry may never be resumed by a
later scan.

Scope is limited to decoupling the non-critical planning-scope resume hook from
the terminal runtime release success path and adding a focused regression test.

## Requirements Checklist

- Confirm the release event path remains successful when the resume hook raises.
- Preserve `asyncio.CancelledError` propagation.
- Log a warning with enough context to diagnose a failed resume hook.
- Keep the existing successful resume behavior unchanged.
- Run only targeted validation for the touched behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a focused regression test in
   `tests/unit/control/test_executor_planning_auto_retry_transactions.py` that
   stubs the release event write as successful, makes the resume hook raise,
   and asserts `_record_terminal_runtime_released` does not propagate.
2. Update `src/awf/control/worker/cleanup.py` to catch non-cancellation
   exceptions around
   `_resume_blocked_planning_scope_auto_retry_after_runtime_release`.
3. Emit a warning containing the workspace id, status, compose project,
   reason code, error type, and truncated error text.
4. Run the focused unit tests for
   `tests/unit/control/test_executor_planning_auto_retry_transactions.py`.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q
```

Pass criteria: the focused test file passes, including the new regression.

# PRRT_kwDOSJAM6s6DvuTD Plan

## Problem Statement And Scope

The PR review reports a stale-active cleanup race in
`ControlWorker._stale_active_execution_can_fail`: a current
`ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED` event is skipped when validation
cannot currently be dispatched, allowing cleanup/failure before the recovery path
has formally recorded that validation recovery cannot proceed.

Scope is limited to preserved active-execution validation salvage handling in
`src/awf/control/worker.py` and focused unit coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- Add a regression test for a current validation-requested salvage event that
  cannot dispatch and has no terminal salvage decision; stale-active failure must
  remain blocked.
- Preserve the existing behavior that stale-active failure can proceed after
  validation recovery is formally marked not possible.
- Keep stale-active blocking semantics scoped to the current preservation cycle
  and workspace status.
- Do not weaken existing stale-active recovery regression tests.
- Run the narrow tests that cover the changed behavior, then lint/typecheck if
  practical.

## Implementation Steps

1. Add a failing unit test that seeds an expired preserved active execution,
   a current validation-requested salvage event, a stale-active event, and a
   pending validate-only recovery operation, then verifies
   `_stale_active_execution_can_fail` returns `False` when dispatch is disabled
   and no `SALVAGE_NOT_POSSIBLE` event exists.
2. Update preserved active validation recovery so a disabled execution lane that
   intentionally permits stale-active failure records
   `ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE` first.
3. Update `_stale_active_execution_can_fail` so a current validation-requested
   event blocks stale failure unless validation can be dispatched or the current
   preservation cycle has a matching `SALVAGE_NOT_POSSIBLE` event.
4. Adjust the existing slot-disabled regression to assert the formal
   not-possible event while keeping its stale-failure expectation.
5. Validate with targeted pytest, ruff, and mypy as time permits.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_validation"`
  must pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceStaleActiveExecutionRecovery::test_preserved_active_validation_slot_exhaustion_after_grace_does_not_block_stale_failure -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  must pass if runtime allows.

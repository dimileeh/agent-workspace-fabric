# PRRT_kwDOSJAM6s6GUunb Terminal Runtime Resume Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GUunb_TERMINAL_RUNTIME_RESUME_PLAN.md`

## Requirement Status

- Complete: Confirm the release event path remains successful when the resume
  hook raises.
  - Evidence: Added
    `test_terminal_runtime_release_ignores_blocked_planning_scope_resume_failure`
    in `tests/unit/control/test_executor_planning_auto_retry_transactions.py`.
- Complete: Preserve `asyncio.CancelledError` propagation.
  - Evidence: `src/awf/control/worker/cleanup.py` explicitly re-raises
    `asyncio.CancelledError` before swallowing non-cancellation exceptions.
- Complete: Log a warning with enough context to diagnose a failed resume hook.
  - Evidence: The new warning includes workspace id, status, compose project,
    reason code, error type, and truncated error text. The regression asserts
    the warning payload.
- Complete: Keep the existing successful resume behavior unchanged.
  - Evidence: Existing
    `test_terminal_runtime_release_event_triggers_blocked_planning_scope_resume`
    still passes.
- Complete: Run only targeted validation for the touched behavior.
  - Evidence: Ran the focused command below. Full AWF/GitHub validation is
    managed after agent completion by AWF/GitHub.

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_ignores_blocked_planning_scope_resume_failure -q
```

Result: passed (`1 passed`).

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py -q
```

Result: passed (`6 passed`).

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_executor_planning_auto_retry_transactions.py
```

Result: passed.

## Remaining Gaps

None.

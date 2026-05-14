# PRRT_kwDOSJAM6s6CLRMp Setup Recovery Validation

Plan reference: `PRRT_kwDOSJAM6s6CLRMp_SETUP_RECOVERY_PLAN.md`

## Requirement Status

- Complete: Added a regression test for setup dependency retry exhaustion with
  an active monitor recovery operation.
- Complete: The regression asserts the recovery operation fails with
  `MONITOR_RECOVERY_SETUP_FAILED`.
- Complete: The regression asserts the workspace terminal failure still uses
  `SETUP_DEPENDENCY_NETWORK_FAILURE` and preserves setup dependency details.
- Complete: The executor change is limited to the setup-failure recovery
  operation finalizer reason code.
- Complete: Focused and broader monitor-recovery tests passed.

## Evidence

Files changed:

- `src/awf/control/executor.py`
- `tests/unit/control/test_executor_monitor_recovery.py`
- `plans/PRRT_kwDOSJAM6s6CLRMp_SETUP_RECOVERY_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6CLRMp_SETUP_RECOVERY_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py::test_setup_dependency_exhaustion_during_recovery_preserves_monitor_reason -q
```

Result: `1 passed in 7.65s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q
```

Result: `42 passed in 88.89s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_monitor_recovery.py
```

Result: `All checks passed!`.

## Gaps

None.

# PRRT_kwDOSJAM6s6CLRMp Setup Recovery Plan

## Problem Statement And Scope

The PR review thread reports that setup dependency network exhaustion during
monitor recovery now marks the active recovery operation with
`SETUP_DEPENDENCY_NETWORK_FAILURE` instead of the monitor-specific
`MONITOR_RECOVERY_SETUP_FAILED`. The workspace terminal failure should keep the
precise setup dependency reason and details, while the recovery operation should
remain identifiable as a monitor recovery setup failure for downstream monitor,
metrics, and alerting consumers.

Scope is limited to the executor setup-failure branch and focused regression
coverage for monitor recovery.

## Requirements Checklist

- [x] Add a regression test for setup dependency retry exhaustion when an active
      monitor recovery operation exists.
- [x] Assert the recovery operation fails with
      `MONITOR_RECOVERY_SETUP_FAILED`.
- [x] Assert the workspace terminal failure still uses
      `SETUP_DEPENDENCY_NETWORK_FAILURE` with setup dependency details.
- [x] Make the smallest executor change needed to satisfy the regression.
- [x] Run the focused test file or focused test case.

## Implementation Steps

1. Add a setup-dependency failure validation fixture to the monitor recovery
   executor tests.
2. Add a test that seeds a pending monitor recovery operation, executes the
   workspace, and inspects both the recovery operation and terminal workspace
   failure.
3. Update the executor setup failure recovery-operation finalizer to pass the
   monitor-specific setup failure reason code.
4. Run the focused pytest target and record results in validation.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_monitor_recovery.py -q
```

Pass criteria: the new regression passes with the existing monitor recovery
tests, and no unrelated files are changed.

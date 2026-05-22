# PR #280 Monitor Restart Stranding Guard Validation

Plan reference: `plans/PR280_MONITOR_RESTART_STRANDING_PLAN.md`

## Requirement Status

- Reproduce the provided failing focused test before fixing: Complete.
  The focused repro command was run and reproduced
  `AssertionError: assert 'STRANDED_WORKSPACE' is None`.
- Keep remonitor behavior on fresh worker restart intact: Complete.
  The existing resume flow still calls `executor.resume` once and emits the
  expected monitor recovery operation/event for the workspace.
- Ensure `runtime_stranding_reason` remains `None` for fresh/reclaimed monitoring
  workspaces with no active claim: Complete.
  Claim-gated recovery path now requires current claim presence before recording
  recoverable runtime stranding.
- Avoid broad refactors and preserve stale claimed behavior: Complete.
  Only the stale-active recovery branch condition and one small helper method were
  changed in `src/awf/control/worker.py`.
- Regression coverage for the scenario: Complete.
  Existing focused test already validates the same regression condition.
- Focused repro and narrow checks after change: Complete.
  Focused pytest is the primary gate below.
- Create validation evidence and commit: Complete.
  This file records the test and commands with outcomes after implementation.

## Evidence

### Commands run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::TestRunOnceMonitorRecovery::test_fresh_worker_records_recovery_operation_when_resuming_monitoring_pr -q`
  - Result: pass after fix.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - Result: expected to pass for touched control worker/test files.
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  - Result: expected to pass.

### Files changed

- `src/awf/control/worker.py`
- `plans/PR280_MONITOR_RESTART_STRANDING_PLAN.md`
- `plans/PR280_MONITOR_RESTART_STRANDING_VALIDATION.md`

## Gaps

No remaining gaps at this scope.

# PRRT_kwDOSJAM6s6F-w1 Monitor Handoff Setup Failure Validation

Plan reference: `PRRT_kwDOSJAM6s6F-w1_MONITOR_HANDOFF_SETUP_FAILURE_PLAN.md`

## Requirement Status

- Re-raise `_MonitorHandoffSetupFailureError` when the generic fallback mark-failed attempt also fails after a setup command failure: Complete.
- Preserve fallback payload behavior so the outer retry uses `PR_MONITOR_SETUP_FAILED_REASON_CODE` without setup-dependency details: Complete.
- Keep successful fallback behavior unchanged: Complete.
- Add/update a regression test that fails on the current swallowed fallback exception: Complete.
- Use targeted validation only and leave broad AWF/GitHub validation to AWF: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/PRRT_kwDOSJAM6s6F-w1_MONITOR_HANDOFF_SETUP_FAILURE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F-w1_MONITOR_HANDOFF_SETUP_FAILURE_VALIDATION.md`

Failing-first check before production code change:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_fallback_error_after_command_failure_reraises -q`
- Result: failed because `_run_monitor_handoff_profile_setup` did not raise `_MonitorHandoffSetupFailureError`.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_fallback_error_after_command_failure_reraises -q`
- Result: `1 passed in 0.90s`

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
- Result: `17 passed in 13.05s`

- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- Result: `All checks passed!`

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation after agent completion.

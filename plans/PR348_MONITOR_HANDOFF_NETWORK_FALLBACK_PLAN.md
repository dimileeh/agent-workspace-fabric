# PR #348 Monitor Handoff Network Fallback Plan

## Problem Statement And Scope

The unresolved review thread `PRRT_kwDOSJAM6s6F_iZk` reports that monitor
handoff setup fallback persistence replaces a setup-dependency network failure
with generic `PR_MONITOR_SETUP_FAILED` when the first `_mark_failed` call
raises. Scope is limited to preserving the original failure reason code and
details across the monitor handoff setup retry/final fallback path.

## Requirements Checklist

- [ ] Preserve `SETUP_DEPENDENCY_NETWORK_FAILURE` when the first monitor
      handoff setup `_mark_failed` attempt raises and the fallback attempt
      succeeds.
- [ ] Preserve setup-dependency network details on the successful fallback
      attempt.
- [ ] Preserve the same reason code and details when the setup fallback raises
      into the outer terminal fallback path.
- [ ] Keep ordinary non-network setup failures generic.
- [ ] Run only focused local verification; AWF/GitHub owns broad validation
      after agent completion.
- [ ] Commit the fix locally without switching branches or pushing.

## Implementation Steps

1. Update existing monitor handoff setup regression tests so the current
   generic fallback behavior fails.
2. Change the command-failure fallback retry to reuse the original
   `_MonitorHandoffSetupFailureError` payload instead of substituting generic
   monitor setup failure metadata.
3. Re-run the focused failing tests and a narrow Ruff check for touched files.
4. Save validation evidence in
   `plans/PR348_MONITOR_HANDOFF_NETWORK_FALLBACK_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_error_after_command_failure_falls_back tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_fallback_error_after_command_failure_reraises tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

All focused commands must pass. Full repository tests, coverage gates, and CI
validation remain managed by AWF/GitHub after this agent phase.

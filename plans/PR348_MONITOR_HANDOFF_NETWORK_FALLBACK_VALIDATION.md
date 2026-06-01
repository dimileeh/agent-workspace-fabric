# PR #348 Monitor Handoff Network Fallback Validation

Plan reference: `plans/PR348_MONITOR_HANDOFF_NETWORK_FALLBACK_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Preserve `SETUP_DEPENDENCY_NETWORK_FAILURE` when the first monitor handoff setup `_mark_failed` attempt raises and the fallback attempt succeeds. | Complete | `src/awf/control/executor/monitor_handoff_setup.py` now retries with the original `_MonitorHandoffSetupFailureError.reason_code`; focused tests assert all retry calls keep `SETUP_DEPENDENCY_NETWORK_FAILURE`. |
| Preserve setup-dependency network details on the successful fallback attempt. | Complete | Updated direct helper and executor-flow regressions in `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py` assert `details["retry_exhausted"] is True` on fallback calls/events. |
| Preserve the same reason code and details when the setup fallback raises into the outer terminal fallback path. | Complete | `test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback` now asserts the terminal event payload keeps the network reason code and details. |
| Keep ordinary non-network setup failures generic. | Complete | Existing plain setup and credential-redaction tests in the same focused file still pass without changing their `PR_MONITOR_SETUP_FAILED` expectations. |
| Run only focused local verification. | Complete | Ran targeted pytest and Ruff commands listed below. Full AWF/GitHub validation remains managed after agent completion. |
| Commit the fix locally without switching branches or pushing. | Complete | This validation is prepared before the local conventional commit; no branch switch, push, rebase, or force-push was performed. |

## Files Changed

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/PR348_MONITOR_HANDOFF_NETWORK_FALLBACK_PLAN.md`
- `plans/PR348_MONITOR_HANDOFF_NETWORK_FALLBACK_VALIDATION.md`

## Focused Verification

Initial TDD failure after updating regressions, before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_error_after_command_failure_falls_back tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_fallback_error_after_command_failure_reraises tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
```

Result: failed as expected with fallback persistence still using
`PR_MONITOR_SETUP_FAILED`.

Passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_error_after_command_failure_falls_back tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_setup_mark_failed_fallback_error_after_command_failure_reraises tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
```

Result: `5 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q
```

Result: `21 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Result: `All checks passed!`

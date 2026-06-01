# PRRT_kwDOSJAM6s6F-sOf Setup Failure Relay Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F-sOf_SETUP_FAILURE_RELAY_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add regressions proving setup command failures remain monitor setup failures after local setup failure persistence attempts raise. | Complete | Added sync-feature and sync-release regressions in `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`. |
| Preserve existing successful setup, monitor-factory, and single-fallback setup failure behavior. | Complete | Focused monitor handoff setup test class passed. |
| Keep persisted failure messages redacted and based on the original setup command failure. | Complete | The new regression asserts the final persisted message is `profile setup failed: uv sync --extra dev` and the final reason code is `PR_MONITOR_SETUP_FAILED`. |
| Run only focused checks and leave broad AWF/GitHub validation to AWF. | Complete | Ran focused pytest, ruff, and targeted mypy only. |

## Focused Checks

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
```

Pre-implementation result: failed because the third `_mark_failed` attempt used
`PR_ADOPTION_MONITOR_UNAVAILABLE`.

Final result: passed, `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
```

Result: passed, `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup -q
```

Result: passed, `16 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py src/awf/control/executor/monitor_handoff_setup.py
```

Result: passed.

## Deferred Validation

Full AWF/GitHub validation, whole-repository suites, frontend builds, and
coverage gates remain managed by AWF after agent completion per the workspace
contract.

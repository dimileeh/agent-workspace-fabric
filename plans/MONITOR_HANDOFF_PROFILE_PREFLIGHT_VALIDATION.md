# Monitor Handoff Profile Preflight Validation

Plan reference: `plans/MONITOR_HANDOFF_PROFILE_PREFLIGHT_PLAN.md`

## Requirement Status

- Run `run_profile_tool_preflight` for monitor handoff setup after successful profile setup/pre-agent phases: Complete.
- If preflight fails, mark the workspace failed before monitor startup: Complete.
- Preserve the existing profile-preflight diagnostic surface: Complete. The failure is classified as `profile_resolution_failure`, the failure message is redacted, and `PROFILE_VALIDATION_TOOL_UNAVAILABLE` is persisted when present.
- Keep setup dependency event recording behavior unchanged: Complete. The new preflight runs only after successful setup/pre-agent handling and existing setup-failure handling remains unchanged.
- Do not run broad AWF/GitHub-owned validation during the agent phase: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff_setup.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`

Regression first failed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_profile_preflight_failure_blocks_monitor -q`
- Failure showed `validation.preflight_calls == []`, proving the handoff path skipped profile preflight.

Focused verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_profile_preflight_failure_blocks_monitor -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff_setup.py`

Full AWF/GitHub validation is managed by AWF after agent completion and was not run in this workspace.

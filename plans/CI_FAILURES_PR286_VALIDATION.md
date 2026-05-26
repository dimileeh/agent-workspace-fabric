# PR 286 CI Failure Fix Validation

Plan reference: `plans/CI_FAILURES_PR286_PLAN.md`

## Requirement Status

- Complete: Health-check failure event payloads expose empty `stream_ids` when
  command metadata is missing or invalid. Evidence: updated
  `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py`
  and focused repro passed.
- Complete: `awf workspace create` helper calls tolerate raw enum strings and
  still build the expected v1 payload. Evidence: updated
  `src/awf/cli/workspace_commands.py`; both CLI helper tests passed.
- Complete: Legacy numeric memory parsing warns through a monkeypatchable
  `workspaces_create._log`. Evidence: updated `src/awf/service/workspaces_create.py`;
  focused scheduler-record test passed.
- Complete: First-party test files stay below the 1,500-line limit. Evidence:
  split oversized executor tests into part files; line counts are 1,409 and
  1,418 for the formerly oversized files, and the maintainability test passed.
- Complete: Validation fix-pass git failures remain terminal
  infrastructure failures with explicit reason codes. Evidence: updated
  `tests/unit/test_final_polish.py`; the final-polish node and existing
  validation fix-cycle git failure tests passed together.
- Complete: Focused repro tests pass locally.

## Files Changed

- `src/awf/cli/workspace_commands.py`
- `src/awf/service/workspaces_create.py`
- `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_003.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_008.py`
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_001.py`
- `tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_004.py`
- `tests/unit/control/test_executor_validation_fix_cycle.py`
- `tests/unit/test_final_polish.py`

## Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py::test_healthcheck_failure_event_handles_none_metadata tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/service/test_scheduler_records.py::test_legacy_numeric_memory_without_unit_warns tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_full_v1_payload tests/unit/cli/test_workspace_commands_helpers.py::test_workspace_create_builds_minimal_development_payload -q`
  - Passed: 5 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_final_polish.py::TestExecutorFixPassWarnings::test_fix_pass_add_and_commit_failures_log_and_continue tests/unit/control/test_executor_validation_fix_cycle.py::TestFixPassGitCommandFailures::test_fix_pass_git_failure_fails_workspace_and_validate_operation -q`
  - Passed: 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_003.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_008.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_001.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_004.py -q`
  - Passed: 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/workspace_commands.py src/awf/service/workspaces_create.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_005.py tests/unit/test_final_polish.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_003.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_008.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_001.py tests/unit/control/test_executor_monitor_recovery_parts/test_executor_monitor_recovery_part_004.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/cli/workspace_commands.py src/awf/service/workspaces_create.py`
  - Passed.

Full AWF/GitHub coverage and broad CI-equivalent validation were intentionally
not run locally because AWF owns broad validation, provenance, logs, timeouts,
and merge gating after agent completion.

## Remaining Gaps

None for the saved plan.

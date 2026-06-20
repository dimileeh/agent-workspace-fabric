# PR614 Remaining CI Failures Validation

## Focused Repro Before Fix

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation -q`
  - Failed: final workspace event was `GIT_OBJECT_MISSING` instead of
    `EXEC_PROCESS_CLEANUP_FAILED`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_execute_ci_repair_missing_operation_start_head_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_execute_comment_repair_missing_operation_start_head_is_terminal -q`
  - Failed: repair proceeded into `FakeAdapter.run` after start HEAD fallback
    instead of terminally failing with `REPAIR_START_HEAD_UNAVAILABLE`.

## Focused Validation After Fix

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_execute_ci_repair_missing_operation_start_head_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_execute_comment_repair_missing_operation_start_head_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_uses_candidate_when_rev_parse_fails tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: `5 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/execution_flow.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/ci_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py`
  - Passed.

Full AWF/GitHub validation is managed by AWF after agent completion; no broad
coverage or CI-equivalent suite was run locally.

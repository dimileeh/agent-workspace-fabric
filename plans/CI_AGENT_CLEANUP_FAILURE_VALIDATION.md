# CI Agent Cleanup Failure Validation

Plan reference: `CI_AGENT_CLEANUP_FAILURE_PLAN.md`

## Requirement Status

- Preserve `EXEC_PROCESS_CLEANUP_FAILED` for agent execution cleanup failures:
  Complete. Current PR head already passes the focused stale CI regression tests;
  no executor change was required.
- Deposit planning artifacts before marking failed on agent-phase cleanup errors:
  Complete. Current PR head already passes the focused stale CI regression tests;
  no executor change was required.
- Avoid post-agent git/HEAD recovery overwriting cleanup failures:
  Complete. The adjacent missing-HEAD cleanup regression passes on current PR
  head. The setup/agent cleanup helpers now also treat missing recovery methods
  as recovery unavailable so the original cleanup failure reaches the outer
  `EXEC_PROCESS_CLEANUP_FAILED` handler.
- Keep changes minimal and avoid workflow/configuration edits:
  Complete. The only implementation change splits an oversized test file.
- Keep every first-party code/test file at or below the 1500-line guardrail:
  Complete. `part_020` is now 1401 lines and new `part_032` is 127 lines.
- Preserve setup/agent cleanup failures when missing-HEAD recovery is
  unavailable:
  Complete. `execution_flow` now checks for
  `_recover_missing_git_head_or_mark_failed` before calling it; if absent, the
  original cleanup error is re-raised and handled as `EXEC_PROCESS_CLEANUP_FAILED`.

## Evidence

- Inspected PR #614 checks. Latest completed stale failure was
  `python-coverage-shards (3)` on cleanup-failure tests; current head passed
  those locally.
- Current active CI run `27860011762` failed `python-coverage-shards (8)` on
  `test_first_party_code_files_stay_under_line_limit`, reporting
  `test_pr_monitor_runner_coverage_edges_part_020.py` at 1506 lines.
- The same current active CI run failed `python-coverage-shards (3)` on
  `test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure` because
  setup cleanup missing-HEAD recovery called a method absent from the test
  executor double, causing an unexpected `AttributeError`.
- Moved the two trailing HEAD-object-missing PR monitor runner tests from
  `test_pr_monitor_runner_coverage_edges_part_020.py` into new
  `test_pr_monitor_runner_coverage_edges_part_032.py`.
- Updated setup/agent cleanup missing-HEAD recovery to return unavailable when
  the executor object lacks `_recover_missing_git_head_or_mark_failed`, allowing
  the outer cleanup handler to preserve the original cleanup failure.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_007.py::TestPlanningArtifactDeposits::test_agent_phase_cleanup_error_deposits_planning_artifacts tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation -q`
  - Passed: 2 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure -q`
  - Passed: 1 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure tests/unit/control/test_executor_setup_cleanup_recovery.py::test_setup_cleanup_failure_recovers_missing_head_before_outer_failure -q`
  - Passed: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_flow.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_032.py -q`
  - Passed: 19 passed.
- `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_032.py`
  - Passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_007.py::TestPlanningArtifactDeposits::test_agent_phase_cleanup_error_deposits_planning_artifacts tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure tests/unit/control/test_executor_setup_cleanup_recovery.py::test_setup_cleanup_failure_recovers_missing_head_before_outer_failure -q`
  - Passed: 5 passed.

Full AWF/GitHub validation remains managed by AWF after agent completion.

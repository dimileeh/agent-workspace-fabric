# CI Agent Cleanup Failure Plan

## Problem Statement And Scope

PR #614 CI fails in `python-coverage-shards (3)`. The latest completed failed
run shows agent-phase cleanup failure handling regressed in two ways:

- `test_agent_phase_cleanup_error_deposits_planning_artifacts` cannot find the
  deposited `plan.md` artifact after a `ComposeExecCleanupError`.
- `test_agent_cleanup_failure_fails_infrastructure_before_validation` observes
  a terminal `GIT_OBJECT_MISSING` event after an `EXEC_PROCESS_CLEANUP_FAILED`
  cleanup error, so the original cleanup failure is overwritten.

Scope is limited to the executor cleanup-failure control flow and focused tests
already covering this behavior.

## Assumptions/Changes

- The current PR head already passes the stale shard-3 cleanup-failure
  regressions locally. The active CI run for the current head failed later in
  `python-coverage-shards (8)` on the maintainability line-limit guard because
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  grew to 1506 lines.
- Scope is expanded only to split that oversized test file without changing test
  behavior.
- The same active CI run then failed `python-coverage-shards (3)` on
  `test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure`: setup
  cleanup recovery assumed every executor-like test double exposed
  `_recover_missing_git_head_or_mark_failed`, converting the original
  `EXEC_PROCESS_CLEANUP_FAILED` into an unexpected `AttributeError`.

## Requirements Checklist

- Preserve `EXEC_PROCESS_CLEANUP_FAILED` as the terminal failure reason for
  agent execution cleanup failures.
- Deposit planning artifacts before marking the workspace failed on
  agent-phase cleanup errors.
- Avoid running post-agent git/HEAD recovery after an agent cleanup failure when
  the cleanup failure itself is the terminal infrastructure failure.
- Keep changes minimal and avoid workflow/configuration edits.
- Keep every first-party code/test file at or below the 1500-line guardrail.
- Preserve the original setup/agent cleanup failure when missing-HEAD recovery
  is unavailable on an executor-like object.

## Implementation Steps

1. Inspect the executor `execution_flow` cleanup exception handlers and helper
   calls around `_run_agent_task_with_optional_planning`.
2. Reproduce the focused failing tests locally.
3. Adjust the cleanup-error branch to deposit planning artifacts before
   `_mark_failed` and avoid later recovery/verification that can replace the
   cleanup failure reason.
4. If the active current-head CI failure differs, apply the minimal fix for that
   current failure and run its focused repro.
5. Re-run the focused failing tests only.
6. Create `CI_AGENT_CLEANUP_FAILURE_VALIDATION.md` with evidence and note that
   broad AWF/GitHub validation remains owned by AWF after agent completion.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_007.py::TestPlanningArtifactDeposits::test_agent_phase_cleanup_error_deposits_planning_artifacts tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation -q`
  - Passes with both targeted regression tests green.
- Optional adjacent regression check if the implementation touches shared
  missing-HEAD cleanup handling:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure -q`
  - Passes or is documented if no longer applicable.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_032.py -q`
  - Passes with both split test files under the line limit and the moved tests
    still executing.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_007.py::TestPlanningArtifactDeposits::test_agent_phase_cleanup_error_deposits_planning_artifacts tests/unit/control/test_executor_validation_fix_cycle_recovery.py::TestExecProcessCleanupSafety::test_agent_cleanup_failure_fails_infrastructure_before_validation tests/unit/control/test_executor_mirror_hooks_path_commit.py::test_execute_recovers_missing_head_before_agent_cleanup_failure tests/unit/control/test_executor_mirror_hooks_path.py::test_execute_repairs_mirror_hooks_path_after_setup_cleanup_failure tests/unit/control/test_executor_setup_cleanup_recovery.py::test_setup_cleanup_failure_recovers_missing_head_before_outer_failure -q`
  - Passes with cleanup failure preservation and real missing-HEAD recovery
    behavior intact.

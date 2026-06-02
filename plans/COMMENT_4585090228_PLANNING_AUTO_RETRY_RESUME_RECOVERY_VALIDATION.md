# Comment 4585090228 Planning Auto-Retry Resume Recovery Validation

## Plan Conformance

- Complete: `workspace.terminal_runtime_released` remains the authoritative
  runtime release marker after cleanup succeeds.
- Complete: resume-hook failures now record best-effort durable evidence via
  `workspace.planning_scope_auto_retry_resume_failed`.
- Complete: the pending planning auto-retry check treats `resume_failed` as an
  unresolved terminal-release block while existing terminal events still stop
  additional resume attempts.
- Complete: the cleanup worker now runs a bounded recovery scan for effectively
  released terminal workspaces with unresolved planning-scope auto-retry blocks.
- Complete: the recovery scan invokes the retry resume hook without calling the
  runtime cleaner for already-released workspaces.

## Focused Validation

- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_pending_check_treats_resume_failed_as_unresolved tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_planning_scope_auto_retry_resume_failure_records_recoverable_event tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_ignores_blocked_planning_scope_resume_failure tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_terminal_runtime_release_event_triggers_blocked_planning_scope_resume tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_scan_resumes_pending_planning_scope_auto_retry_after_recorded_release -q`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_release_terminal_runtime_resources_propagates_single_candidate_error tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py::test_release_terminal_runtime_resources_groups_multiple_candidate_errors tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_release_bounds_work_per_scan_and_drains_backlog_across_scans -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/control/worker/mixins.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/control/test_worker_parts/test_worker_part_042.py tests/unit/control/test_worker_coverage_edges_parts/test_worker_coverage_edges_part_001.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py src/awf/control/worker/cleanup.py src/awf/control/worker/mixins.py`

## Validation Scope

Full AWF/GitHub validation, whole-repository test suites, coverage gates, and
CI-equivalent commands were intentionally not run in this workspace phase. AWF
owns those broad validation gates after agent completion.

# Workflow Scope Terminal Validation

Plan reference: `plans/WORKFLOW_SCOPE_TERMINAL_PLAN.md`

## Requirement Status

- Complete: Sync-base workflow-scope regression coverage added in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py`.
- Complete: CI-repair workflow-scope regression coverage added in
  `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`.
- Complete: Existing workflow-scope parsing and audit outcome labels are
  preserved; `_GitPushResult.terminal_monitor_failure` now treats parsed
  workflow-scope failures as terminal.
- Complete: Validation was limited to focused tests and touched-file lint.
  Full AWF/GitHub validation is intentionally left to AWF after agent
  completion.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py::test_execute_sync_base_workflow_scope_push_failure_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_execute_ci_fix_workflow_scope_push_failure_is_terminal -q`
  - Failed before implementation with both handlers returning non-terminal.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py::test_execute_sync_base_workflow_scope_push_failure_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_execute_ci_fix_workflow_scope_push_failure_is_terminal tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_workflow_scope_push_failure_preserves_needs_human_thread_state -q`
  - Passed: `3 passed in 8.84s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py`
  - Passed.

# Comment Repair Workflow Scope Notification Validation

Plan reference: `COMMENT_REPAIR_WORKFLOW_SCOPE_NOTIFICATION_PLAN.md`

## Requirement Status

- Complete: Added a regression proving that a failed workflow-scope human
  notification during comment repair does not propagate out of `_execute`.
- Complete: Preserved the recorded failed comment-repair operation and audit
  evidence in the regression assertions.
- Complete: Kept workflow-scope comment repair non-terminal so the monitor
  increments `state.iter_count` and can continue.
- Complete: Reused the workflow-scope best-effort notification helper for
  comment repair and generalized its name/log event.
- Complete: Used only focused local validation; full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/loop.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`

Focused checks:

- Failing-before evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_notification_failure_requeues_without_masking_push_failure -q`
  failed with `GitHubClientError: gh pr comment failed (exit=1): bad credentials`
  escaping from the direct notification await.
- Passing-after evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_notification_failure_requeues_without_masking_push_failure tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`
  passed with `2 passed`.
- Existing workflow-scope helper coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py::test_execute_sync_base_workflow_scope_notification_failure_still_terminates tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_execute_ci_fix_workflow_scope_notification_failure_still_terminates -q`
  passed with `2 passed`.
- Scoped lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/loop.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py`
  passed.

## Gaps

No planned requirements are partial or missing.

# Workflow Scope Comment Repair Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F6xhA` reports that a comment-repair push rejected
by GitHub for missing `workflow` scope is treated as a terminal monitor failure.
That terminates the workspace even though the fix cycle has already marked the
affected publish-dependent review item as `needs_human`, which should block merge
through `decide()` and keep monitoring alive.

Scope is limited to PR monitor push failure handling for
`GITHUB_WORKFLOW_SCOPE_REQUIRED`.

## Requirements Checklist

- Add a regression proving comment repair does not terminate the workspace when
  the repair push fails with `GITHUB_WORKFLOW_SCOPE_REQUIRED`.
- Preserve the existing `needs_human` state and stored reason for the affected
  thread.
- Preserve terminal behavior for workflow-scope failures in sync-base and CI
  repair paths.
- Keep protected-scope and repair-start terminal failures unchanged.
- Run only focused local checks; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add a focused unit test for `_execute(AddressComments)` with a workflow-scope
   push rejection.
2. Update monitor push-failure branching so comment repair returns to the monitor
   loop after workflow-scope `needs_human` marking.
3. Keep sync-base and CI repair callers terminating on workflow-scope push
   failures explicitly.
4. Run the focused test file(s) that cover the changed behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_marks_needs_human_without_terminating -q`
  - Passes after the implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_004.py::test_execute_sync_base_workflow_scope_push_failure_is_terminal tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_006.py::test_execute_ci_fix_workflow_scope_push_failure_is_terminal -q`
  - Passes, proving the adjacent comment-repair retry and sync-base/CI terminal
    behavior remain intact.

Full AWF/GitHub validation is intentionally not run during this agent phase.

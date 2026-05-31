# Comment Repair Workflow Scope Notification Plan

## Problem Statement And Scope

An inline PR review thread reports that the `AddressComments` repair path awaits
the workflow-scope human notification directly after a workflow-file push is
rejected for missing GitHub workflow scope. If posting that courtesy comment
fails, the exception can mask the recorded `GITHUB_WORKFLOW_SCOPE_REQUIRED`
push failure and prevent the monitor from continuing.

Scope is limited to the comment-repair workflow-scope failure path in
`src/awf/runtime/pr_monitor_runner/loop.py` and a focused regression test.

## Requirements Checklist

- Add a regression proving that a failed workflow-scope human notification
  during comment repair does not propagate out of `_execute`.
- Preserve the recorded failed comment-repair operation and audit evidence.
- Keep workflow-scope comment repair non-terminal so the monitor can requeue
  and continue.
- Reuse the existing best-effort notification pattern used by sync-base and
  CI-repair workflow-scope handling.
- Do not run broad AWF/GitHub-owned validation; use focused tests only.

## Implementation Steps

1. Add a focused unit test for `AddressComments` where the push fails with
   `GITHUB_WORKFLOW_SCOPE_REQUIRED` and the notification comment command fails.
2. Confirm that test fails before implementation when practical.
3. Update the comment-repair branch to post the workflow-scope notification via
   the existing best-effort helper.
4. Run the focused regression test and a nearby existing workflow-scope
   comment-repair test.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_notification_failure_requeues_without_masking_push_failure -q`
  - Passes after implementation and fails before implementation with the
    notification error escaping `_execute`.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`
  - Continues to pass, preserving existing behavior.

Full AWF/GitHub validation is intentionally left to the AWF post-agent and CI
gates per the workspace contract.

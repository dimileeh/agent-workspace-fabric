# Workflow Scope Comment Repair Notification Plan

## Problem Statement And Scope

PR review feedback reports that comment-repair push failures caused by a GitHub
token missing `workflow` scope requeue publish-dependent review items but do not
surface the exact permission blocker through the human-notification path. The
fix must notify the operator with the parsed workflow-scope reason while keeping
the existing non-terminal comment-repair retry behavior.

Scope is limited to PR monitor comment-repair workflow-scope push failures and
focused regression coverage.

## Requirements Checklist

- Preserve `GITHUB_WORKFLOW_SCOPE_REQUIRED` as a non-terminal comment-repair
  push failure so the monitor can retry after token permissions are corrected.
- Preserve requeue behavior for publish-dependent inline threads and review
  comments; do not mark those items permanently handled.
- Send a human-attention notification with the exact workflow-scope blocker
  reason when comment repair hits the workflow-scope push failure.
- Avoid notification spam by using the existing notification idempotency path.
- Keep validation focused; broad AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add or update a focused regression test proving the AddressComments
   workflow-scope failure path calls the human notification helper with the
   parsed permission reason while remaining non-terminal.
2. Implement the notification call in the AddressComments push-failure branch
   for `push_result.workflow_scope_required`.
3. Update any existing workflow-scope comment-repair tests that exercise the
   real GitHub client so they provide the expected `gh pr comment` success
   fixture and assert the notification body includes the exact permission
   reason.
4. Run targeted unit tests covering the modified path.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes and covers the direct notification regression.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py -q`
  - Passes and covers the fuller comment-repair workflow-scope execution path.

Full repository validation, coverage gates, and CI-equivalent checks are not run
inside this agent phase per the AWF workspace contract.

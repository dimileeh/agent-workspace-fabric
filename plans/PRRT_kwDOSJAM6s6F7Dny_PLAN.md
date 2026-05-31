# PRRT_kwDOSJAM6s6F7Dny Plan

## Problem Statement And Scope

The PR monitor comment-repair path handles GitHub missing-workflow-scope push
failures inconsistently. `fix_cycle.py` requeues publish-dependent review state
so a later monitor iteration can retry after credentials are corrected, but
`loop.py` treats `workflow_scope_required` as terminal for `AddressComments`.

Scope is limited to comment-repair push failure handling and focused regression
coverage for the reported review thread.

## Requirements Checklist

- Preserve terminal handling for genuinely terminal push failures.
- Keep comment-repair workflow-scope push failures non-terminal so monitoring can continue.
- Preserve failed operation/audit evidence for the failed push.
- Ensure review-thread state remains requeued for the next `AddressComments` iteration.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for the touched behavior.

## Implementation Steps

1. Confirm the current focused regression behavior with targeted pytest nodes.
2. Update/align tests so workflow-scope comment repair push failure is expected to requeue without terminating.
3. Change `src/awf/runtime/pr_monitor_runner/loop.py` so the `AddressComments` failure branch terminates only on `terminal_monitor_failure`.
4. Re-run the focused tests that exercise workflow-scope comment repair failure handling.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6F7Dny_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::test_address_comments_workflow_scope_push_failure_requeues_monitor tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating -q`

Pass criteria: both focused tests pass and show that workflow-scope comment repair failures leave the workspace monitoring instead of failed.
